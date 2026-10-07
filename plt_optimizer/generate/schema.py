"""Schema definitions for YAML job specification parsing and validation.

This module provides Pydantic models that define the data contract for
job specifications used by the generate pipeline. It handles:
- Parsing YAML files into typed Python objects
- Top-down inheritance via two-tier mixins (TextAttributes, LabelAttributes)
- Root-level single-label jobs (no explicit `labels` list required)

Example:
    >>> job = parse_yaml("tests_deps/sample_spec.yaml")
    >>> print(job.job_name)
    'Control Panel Tags - Batch 01'
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from plt_optimizer.generate.font_registry import (
    DEFAULT_FONT_NAME,
    normalize_font_name,
)


class HoleLocation(str, Enum):
    """Enumeration of valid hole locations on a label.

    Besides the eight atomic positions (four edges and four corners), two
    group shorthands are accepted for the most common drilling patterns:
    ``corners`` (all four corner holes) and ``sides`` (the left and right
    edge holes). Groups are expanded into their atomic members at schema
    validation time (see :data:`HOLE_LOCATION_GROUPS`), so downstream
    consumers only ever see the eight atomic locations.

    Attributes:
        left: Hole on the left edge.
        right: Hole on the right edge.
        top: Hole on the top edge.
        bottom: Hole on the bottom edge.
        top_left: Hole at the top-left corner.
        top_right: Hole at the top-right corner.
        bottom_left: Hole at the bottom-left corner.
        bottom_right: Hole at the bottom-right corner.
        corners: Group shorthand for all four corner holes.
        sides: Group shorthand for the left and right edge holes.
    """

    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"
    TOP_LEFT = "top-left"
    TOP_RIGHT = "top-right"
    BOTTOM_LEFT = "bottom-left"
    BOTTOM_RIGHT = "bottom-right"
    CORNERS = "corners"
    SIDES = "sides"


# Group shorthand locations mapped to the atomic locations they expand to.
# Expansion preserves the order listed here and replaces the group entry
# in place within the ``holes`` list (see :meth:`HoleSpec.expand`).
HOLE_LOCATION_GROUPS: dict[HoleLocation, tuple[HoleLocation, ...]] = {
    HoleLocation.CORNERS: (
        HoleLocation.TOP_LEFT,
        HoleLocation.TOP_RIGHT,
        HoleLocation.BOTTOM_LEFT,
        HoleLocation.BOTTOM_RIGHT,
    ),
    HoleLocation.SIDES: (HoleLocation.LEFT, HoleLocation.RIGHT),
}


# Default drill-hole diameter (inches) used when a hole specification omits
# ``diameter``. 0.125" is the common drill size across the example jobspecs.
DEFAULT_HOLE_DIAMETER: float = 0.125


class HoleSpec(BaseModel):
    """Specification for a hole to be drilled in a label.

    ``location`` is the primary (and only required) field: the common case
    is a standard 0.125" drill hole, which can be written as ``location``
    alone. ``diameter`` is an optional override for non-standard sizes.

    Group locations (``corners`` / ``sides``) are expanded into their
    atomic member holes at validation time by the ``holes`` field
    validator on :class:`LabelAttributes`; each expanded member inherits
    this spec's ``diameter``.

    Attributes:
        location: The position of the hole on the label edge. Group
            shorthands ``corners`` (all four corners) and ``sides``
            (left + right) are accepted and expanded into their members.
        diameter: The diameter of the hole in inches. Defaults to
            :data:`DEFAULT_HOLE_DIAMETER` (0.125"); must be positive.
    """

    location: HoleLocation = Field(
        description=(
            "Hole position on the label edge. Group shorthands 'corners' "
            "(all four corners) and 'sides' (left + right) expand into their "
            "atomic members at validation time."
        )
    )
    diameter: float = Field(
        default=DEFAULT_HOLE_DIAMETER,
        gt=0.0,
        description=(
            "Hole diameter in inches (default 0.125). A job-config.json "
            "'hole_diameter' fills entries that omit this field."
        ),
    )

    def expand(self) -> list[HoleSpec]:
        """Expand a group location into its atomic hole specifications.

        Returns:
            A single-element list containing this spec for atomic
            locations; for group locations (``corners`` / ``sides``), one
            clone per member location (in :data:`HOLE_LOCATION_GROUPS`
            order), each preserving this spec's diameter.
        """
        members = HOLE_LOCATION_GROUPS.get(self.location)
        if members is None:
            return [self]
        return [self.model_copy(update={"location": member}) for member in members]


class TextHAlignment(str, Enum):
    """Enumeration of valid horizontal text alignment modes for a text line.

    The alignment anchors the rendered line within the label's inner content
    area (label width minus both margins). ``left`` places the line's
    left-most point precisely at the left margin; ``right`` places the
    line's right-most point precisely at the right margin; ``center``
    (the default) centers the line horizontally.

    Attributes:
        LEFT: Left edge of the line sits at the left margin.
        CENTER: Line is horizontally centered within the inner content area.
        RIGHT: Right edge of the line sits at the right margin.
    """

    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


# Canonical stroke-color layer names mapped to their single-letter
# abbreviations (EngraveLab/Vision Pro-style; black is "k" following the
# CMYK convention). Used both to resolve abbreviations on input
# (:meth:`TextColor._missing_`) and to tag color-split output files
# (see ``plt_optimizer.generate.vectorize.export_per_cutter_plts``).
TEXT_COLOR_ABBREVIATIONS: dict[str, str] = {
    "cyan": "c",
    "magenta": "m",
    "yellow": "y",
    "black": "k",
    "red": "r",
    "green": "g",
    "blue": "b",
    "violet": "v",
    "orange": "o",
    "pink": "p",
    "teal": "t",
    "none": "n",
}

# Reverse lookup (abbreviation -> canonical name) for input normalization.
_TEXT_COLOR_BY_ABBREVIATION: dict[str, str] = {
    letter: name for name, letter in TEXT_COLOR_ABBREVIATIONS.items()
}


class TextColor(str, Enum):
    """Enumeration of stroke-color layer tags for text lines and labels.

    The color carries no visual meaning in the emitted PLT: it is a layer
    tag that splits otherwise-identical text into separate toolpaths (one
    HPGL ``SP`` layer and one PLT file per color), so the cutter depth can
    be changed between runs to expose a different material layer color on
    3-layer stock. This mirrors the EngraveLab/Vision Pro workflow where a
    different stroke color forces a separate toolpath.

    Values may be written as the full name (``cyan``) or as the
    single-letter abbreviation (``c``); both forms are case-insensitive.
    ``none`` is the implicit default for every line that does not declare
    a color and must never be specified explicitly (see the ``text_color``
    field validator): users pick a real color name to force a toolpath
    split.

    Attributes:
        cyan: Cyan stroke-color layer (abbreviation ``c``).
        magenta: Magenta stroke-color layer (abbreviation ``m``).
        yellow: Yellow stroke-color layer (abbreviation ``y``).
        black: Black stroke-color layer (abbreviation ``k``, CMYK style).
        red: Red stroke-color layer (abbreviation ``r``).
        green: Green stroke-color layer (abbreviation ``g``).
        blue: Blue stroke-color layer (abbreviation ``b``).
        violet: Violet stroke-color layer (abbreviation ``v``).
        orange: Orange stroke-color layer (abbreviation ``o``).
        pink: Pink stroke-color layer (abbreviation ``p``).
        teal: Teal stroke-color layer (abbreviation ``t``).
        none: Implicit default for text that declares no color; cannot be
            specified explicitly.
    """

    CYAN = "cyan"
    MAGENTA = "magenta"
    YELLOW = "yellow"
    BLACK = "black"
    RED = "red"
    GREEN = "green"
    BLUE = "blue"
    VIOLET = "violet"
    ORANGE = "orange"
    PINK = "pink"
    TEAL = "teal"
    NONE = "none"

    @classmethod
    def _missing_(cls, value: object) -> Optional[TextColor]:
        """Normalize full names and abbreviations case-insensitively.

        Args:
            value: The raw input that failed exact value matching.

        Returns:
            The matching member for a case-insensitive full name or a
            (case-insensitive) single-letter abbreviation; ``None`` for
            anything else, which re-raises the standard enum error.
        """
        if not isinstance(value, str):
            return None
        key = value.strip().lower()
        key = _TEXT_COLOR_BY_ABBREVIATION.get(key, key)
        for member in cls:
            if member.value == key:
                return member
        return None

    @property
    def abbreviation(self) -> str:
        """Return the single-letter abbreviation of this color.

        Returns:
            The one-letter tag (``k`` for black, ``m`` for magenta, ...)
            used in color-split output file names.
        """
        return TEXT_COLOR_ABBREVIATIONS[self.value]


class LayoutMode(str, Enum):
    """Enumeration of plate fill-order preferences for bin packing.

    ``rectpack`` has no sort option that controls fill order directly (the
    emergent order is a side effect of placement tie-breaking), so
    ``columns`` is realized by packing in a transposed frame with a
    bottom-row fill algorithm and mapping every placement back into plate
    space (see ``plt_optimizer.generate.layout``).

    Attributes:
        ROWS: Fill the plate width first, then extend downward (row-major;
            the historical packing behaviour).
        COLUMNS: Fill the plate height first, then extend rightward
            (column-major; the default).
    """

    ROWS = "rows"
    COLUMNS = "columns"


# Default plate fill order: column-major, so labels stack up the plate
# height and only extend rightward as far as necessary.
DEFAULT_LAYOUT_MODE: LayoutMode = LayoutMode.COLUMNS


def normalize_material(value: Optional[str]) -> Optional[str]:
    """Normalize a ``material`` name (shared by every level declaring it).

    Trims surrounding whitespace and rejects whitespace-only values. The
    first-declared spelling is preserved for display; grouping uses
    :func:`material_key` (case-insensitive).

    Args:
        value: The declared material name, or ``None`` (unset).

    Returns:
        The trimmed material name, or ``None`` when unset.

    Raises:
        ValueError: If the value is whitespace-only.
    """
    if value is None:
        return None
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("'material' cannot be empty or whitespace-only")
    return trimmed


def material_key(value: Optional[str]) -> Optional[str]:
    """Return the case-insensitive grouping key for a ``material`` name.

    Two materials are considered the same when their keys match: the value
    is trimmed and case-folded (``"WB(UV)"`` and ``"wb(uv)"`` group
    together). ``None`` (unset) maps to ``None``.

    Args:
        value: A normalized material name, or ``None`` (unset).

    Returns:
        The case-folded key, or ``None`` when unset.
    """
    if value is None:
        return None
    return value.strip().casefold()


class TextAttributes(BaseModel):
    """Attributes that can cascade down to individual text lines.

    These fields are safe to inherit at the TextLine level because they
    describe typographic properties that apply to rendered glyphs.

    Attributes:
        text_height: Optional font height in inches.
        font: Optional font name selecting the glyph outlines used to render
            text. Valid values are the PLT-extracted font keys of
            ``Fonts/plt_fonts.json`` (rendered arc-native) and the basenames
            of ``*.ttf`` files under ``Fonts/`` (extension stripped, rendered
            through ftext); matching is case-insensitive and the value is
            canonicalized at validation time. Cascades line -> label -> job
            (fallback ``"ReliefSingleLineCAD-Regular"``). Unknown names are
            rejected with the full valid list. Run
            ``docs/schema/generate_schema_docs.py --show-fonts`` for the
            current list.
        character_spacing: Optional extra spacing between characters in inches.
        line_spacing: Optional extra spacing between text lines in inches,
            or ``"auto"`` to calculate spacing automatically. When ``"auto"``,
            spacing is calculated such that all lines have equal spacing.
            If ``v_margin`` is explicitly specified, the specified v_margin
            is honored and line_spacing is calculated to fill the remaining
            space. If ``v_margin`` is not explicitly specified, the functional
            v_margin equals the inter-line spacing. Cutter widths of the top
            and bottom lines are considered: half the respective cutter width
            is added to the functional v_margin to ensure correct placement
            in the engraved output. Cascades line -> label -> job (fallback 0.1).
        max_h_compress: Optional maximum horizontal compression fraction in
            ``[0.0, 1.0]``. When a rendered line is wider than the label's
            inner content area, the line may be uniformly compressed
            horizontally down to ``(1 - max_h_compress)`` of its natural
            width. ``0.0`` (the default) disables compression; ``0.5`` allows
            squeezing wide lines to 50% of their natural width. Cascades
            line -> label -> job (and is accepted on plates for schema
            parity, where it is not currently applied during rendering).
        text_h_alignment: Optional horizontal alignment of a rendered text
            line within the label's inner content area. One of ``left``,
            ``center`` or ``right``. ``left`` places the line's left-most
            point precisely at the left margin; ``right`` places the right-
            most point at the right margin; ``center`` (the default)
            centers the line. Cascades line -> label -> job (and is
            accepted on plates for schema parity, where it is not currently
            applied during rendering).
        min_hole_margin: Optional minimum hole margin in inches; during
            text-hole collision avoidance, hole margins will not shrink
            below this value. ``None`` (the default) means collision
            avoidance may reduce the hole margin all the way to ``0.0``
            (hole tangent to the label edge). Cascades line -> label ->
            job (and is accepted on plates for schema parity, where it is
            not currently applied during rendering).
        hole_text_collision_distance: Optional minimum air gap in inches
            between the *engraved* text stroke and the *engraved* drill
            hole stroke. A (line, hole) pair collides when the geometric
            gap between the text bounding box and the hole circle is
            below ``0.5 * (hole_cutter + text_cutter) +
            hole_text_collision_distance``: the first term is the stroke
            floor at which the two cut strokes just touch, and this field
            adds free air between them. Defaults to ``0.15`` inches; an
            explicit ``0.0`` is honored (strokes may touch but never
            overlap). Cascades label -> job (and is accepted on text
            lines and plates for schema parity, where it is not applied
            at that level).
        space_width_fraction: Optional space advance as a fraction of the
            rendered text height (PLT-extracted fonts): a space advances
            ``space_width_fraction * text_height + character_spacing``.
            Defaults to ``0.3``; only ``None`` means unset (an explicit
            ``0.0`` collapses the space to bare ``character_spacing``).
            Cascades line -> label -> job (and is accepted on plates for
            schema parity, where it is not applied at that level).
        min_glyph_width: Optional global minimum glyph advance width in
            inches for PLT-extracted fonts. The profile-envelope kerning
            floors each pair's advance at this value, capped by the left
            glyph's own bounding-box width, so zero-width glyphs (``!``,
            ``|``) still reserve real air without inflating thin-glyph
            pairs past their own silhouette.
            Defaults to ``0.0`` (pure envelope kerning); only
            ``None`` means unset. Cascades line -> label -> job (and is
            accepted on plates for schema parity, where it is not applied
            at that level).
        kerning_window_fraction: Optional kerning window for PLT-extracted
            fonts, in ``[0.0, 1.0]``, as a fraction of the rendered text
            height. Each profile-envelope sample compares against the
            opposite silhouette within +/- (half of this fraction) of the
            text height, so staggered pokes (the glyphs approaching each
            other at slightly different heights) are detected and can
            only widen the advance.
            Defaults to ``0.05``; only ``None`` means unset (an explicit
            ``0.0`` reproduces the historical same-height maximum-
            penetration kerning). Cascades line -> label -> job (and is
            accepted on plates for schema parity, where it is not applied
            at that level).
        kerning_penetration_scale: Optional multiplier on the detected
            profile-envelope penetration for PLT-extracted fonts. ``1.0``
            keeps the geometric penetration; ``>1.0`` over-kerns tight
            pairs proportionally. Defaults to ``1.0``; only ``None`` means
            unset. Cascades line -> label -> job (and is accepted on
            plates for schema parity, where it is not applied at that
            level).
        kerning_recession_scale: Optional multiplier on a *recessed*
            (negative) detected profile-envelope penetration for
            PLT-extracted fonts. ``1.0`` keeps the geometric recession
            (the historical linear behaviour, where lowering
            ``kerning_penetration_scale`` also pulled gapped pairs
            closer); ``0.0`` makes a recessed pair advance by nothing
            beyond the ``min_glyph_width`` floor, so the penetration dial
            only tightens pairs whose silhouettes overlap. Defaults to
            ``1.0``; only ``None`` means unset. Cascades line -> label ->
            job (and is accepted on plates for schema parity, where it is
            not applied at that level).
        kerning_min_gap: Optional extra air in inches added to every
            kerned character pair advance for PLT-extracted fonts, on top
            of the cutter-diameter + character-spacing clearance.
            Defaults to ``0.0`` (no extra gap); only ``None`` means unset.
            Cascades line -> label -> job (and is accepted on plates for
            schema parity, where it is not applied at that level).
        fallback_advance_fraction: Optional multiplier on the
            bounding-box-width fallback advance used for PLT-extracted
            glyph pairs without overlapping height (or lacking envelopes).
            Defaults to ``1.0`` (the left glyph's own width); only ``None``
            means unset. Cascades line -> label -> job (and is accepted on
            plates for schema parity, where it is not applied at that
            level).
        cutter_size: Optional cutter diameter in inches to use for the text
            instead of the tool auto-selected from ``text_height``. When set,
            the requested diameter is snapped to the shop inventory
            (``tools.json`` ``available_cutters``: next size down, else next
            size up) and the rendered toolpath height is recomputed as
            ``text_height - cutter_size`` (the nominal ``text_height`` itself
            is untouched, so vertical fit math is unaffected). ``None`` (the
            default) means unset: the cutter is chosen automatically from
            ``text_height`` (current behaviour). Must be ``> 0``; a value at
            or above the line's ``text_height`` is a resolution error. There
            is deliberately **no** ``job-config.json`` counterpart. Cascades
            line -> label -> job (and is accepted on plates for schema
            parity, where it is not applied at that level).
        cutter_downsize: Optional permission to *reduce* the automatically
            selected cutter when the rendered line has to be compressed
            horizontally. When enabled (the default), a line whose effective
            horizontal scale falls below the midpoint between its current
            cutter and the next smaller inventory tool swaps to that smaller
            tool and re-renders at ``text_height - smaller_cutter`` (the
            nominal ``text_height`` stays the user's intent). The swap is
            one-way: a downsized cutter is never enlarged again within the
            same line. An explicit ``cutter_size`` at any level overrides
            this entirely. Defaults to ``True``; only ``None`` means unset.
            Cascades line -> label -> job (and is accepted on plates for
            schema parity, where it is not applied at that level).
        max_cutter_downsizes: Optional ceiling on how many successive cutter
            downsizings :attr:`cutter_downsize` may apply to one text line
            (``0`` disables the mechanism, ``1`` = at most one size down).
            Each step re-measures the line, because a smaller cutter renders
            *wider* and can justify a further step. Defaults to ``1``; only
            ``None`` means unset. Cascades line -> label -> job (and is
            accepted on plates for schema parity, where it is not applied at
            that level).
        text_color: Optional stroke-color layer tag used to split
            otherwise-identical text into separate toolpaths (one HPGL
            ``SP`` layer and one PLT file per distinct color), so the
            cutter depth can be changed between runs on 3-layer material
            (EngraveLab/Vision Pro-style stroke colors). Accepted on text
            lines and labels only -- it is deliberately NOT cascaded
            (unset resolves to ``"none"`` and never reads a parent
            value), and a job-level value is rejected. Values are
            :class:`TextColor` full names or single-letter abbreviations,
            both case-insensitive (``cyan`` / ``c`` ...). ``none`` is the
            implicit default and cannot be specified explicitly.
    """

    text_height: Optional[float] = Field(
        default=None,
        description="Font height in inches. Cascades line -> label -> job (fallback 0.25).",
    )
    font: Optional[str] = Field(
        default=None,
        description=(
            "Font name: a PLT-extracted font key (Fonts/plt_fonts.json, "
            "rendered arc-native with arcs preserved) or a TrueType basename "
            "(*.ttf under Fonts/, extension stripped); case-insensitive, "
            "canonicalized at validation. Cascades line -> label -> job "
            f"(fallback {DEFAULT_FONT_NAME}). Unknown names are rejected; run "
            "docs/schema/generate_schema_docs.py --show-fonts for the full "
            "valid list."
        ),
    )
    character_spacing: Optional[float] = Field(
        default=None,
        description=(
            "Extra spacing between characters in inches (fallback: 1.5x the "
            "resolved cutter diameter)."
        ),
    )
    line_spacing: Optional[Union[float, Literal["auto"]]] = Field(
        default=None,
        description=(
            "Extra spacing between text lines in inches, or "
            "'auto' to calculate spacing automatically based on label height, "
            "margins, and text line count (default when unspecified). Fallback when "
            "not specified at any level is 'auto'."
        ),
    )
    max_h_compress: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Maximum horizontal compression fraction in [0.0, 1.0].",
    )
    text_h_alignment: Optional[TextHAlignment] = Field(
        default=None,
        description="Horizontal text alignment: left, center, or right.",
    )
    min_hole_margin: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Minimum hole margin in inches; hole margins will not shrink "
            "below this value during collision avoidance."
        ),
    )
    hole_text_collision_distance: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Minimum air gap in inches between engraved text and drill "
            "hole strokes, on top of the stroke floor "
            "0.5 * (hole_cutter + text_cutter)."
        ),
    )
    space_width_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Space advance as a fraction of the rendered text height "
            "(PLT-extracted fonts): a space advances "
            "space_width_fraction * text_height + character_spacing. "
            "Cascades line -> label -> job (fallback 0.3); explicit 0.0 "
            "is honored."
        ),
    )
    min_glyph_width: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Global minimum glyph advance width in inches (PLT-extracted "
            "fonts): clamps the profile-envelope kerning so zero-width "
            "glyphs still reserve real air. Cascades line -> label -> job "
            "(fallback 0.0 = pure envelope kerning); explicit 0.0 is honored."
        ),
    )
    kerning_window_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Kerning window for PLT-extracted fonts as a fraction of the "
            "rendered text height in [0.0, 1.0]: each envelope sample "
            "compares against the opposite silhouette within +/- (half of "
            "this fraction) of the text height and the effective "
            "penetration is the worst windowed penetration, so staggered "
            "pokes widen the advance. "
            "Cascades line -> label -> job (fallback 0.05); explicit 0.0 "
            "is honored (= historical same-height kerning)."
        ),
    )
    kerning_penetration_scale: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Multiplier on the profile-envelope penetration detected for "
            "PLT-extracted fonts: 1.0 keeps the geometric penetration, "
            ">1.0 over-kerns tight pairs "
            "proportionally. Cascades line -> label -> job (fallback 1.0); "
            "explicit 0.0 is honored (ignores detected closeness entirely)."
        ),
    )
    kerning_recession_scale: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Multiplier on a *recessed* (negative) profile-envelope "
            "penetration for PLT-extracted fonts: 1.0 keeps the geometric "
            "recession (the historical linear behaviour, where lowering "
            "kerning_penetration_scale also pulled gapped pairs closer), "
            "0.0 makes a recessed pair advance by nothing beyond the "
            "min_glyph_width floor. Cascades line -> label -> job "
            "(fallback 1.0); explicit 0.0 is honored."
        ),
    )
    kerning_min_gap: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Extra air in inches added to every kerned character pair "
            "advance (PLT-extracted fonts), on top of the cutter diameter "
            "and character_spacing clearance. Cascades line -> label -> job "
            "(fallback 0.0 = no extra gap); explicit 0.0 is honored."
        ),
    )
    fallback_advance_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Multiplier on the bounding-box-width fallback advance used for "
            "PLT-extracted glyph pairs without overlapping height (or "
            "lacking envelopes). Cascades line -> label -> job (fallback "
            "1.0 = the left glyph's own width); explicit 0.0 is honored."
        ),
    )
    cutter_size: Optional[float] = Field(
        default=None,
        gt=0.0,
        description=(
            "Cutter diameter in inches to use for this text instead of the "
            "tool auto-selected from text_height. Snapped to the shop "
            "inventory (tools.json available_cutters: next size down, else "
            "next size up); the rendered toolpath height becomes "
            "text_height - cutter_size. Omit (null) to keep automatic "
            "cutter selection from text_height. Must be > 0 and below the "
            "line's text_height. No job-config.json counterpart. Cascades "
            "line -> label -> job."
        ),
    )
    cutter_downsize: Optional[bool] = Field(
        default=None,
        description=(
            "Allow the automatically selected cutter to be reduced when the "
            "text line is horizontally compressed: a line squeezed past the "
            "midpoint toward the next smaller inventory tool swaps to it and "
            "re-renders at text_height - smaller_cutter. One-way (a downsized "
            "cutter is never enlarged again within the same line) and fully "
            "overridden by an explicit cutter_size. Requires a tools.json "
            "available_cutters inventory. Cascades line -> label -> job "
            "(fallback true); explicit false is honored."
        ),
    )
    max_cutter_downsizes: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Maximum number of successive cutter downsizings applied to one "
            "text line (0 disables the mechanism, 1 = at most one size down). "
            "Each step re-measures the line, since a smaller cutter renders "
            "wider. Cascades line -> label -> job (fallback 1); explicit 0 is "
            "honored."
        ),
    )
    text_color: Optional[TextColor] = Field(
        default=None,
        description=(
            "Stroke-color layer tag splitting otherwise-identical text into "
            "separate toolpaths (labels and text lines only; rejected at the "
            "job level; never cascades). Full name or case-insensitive "
            "single-letter abbreviation (c, m, y, k, r, g, b, v, o, p, t); "
            "'none' is the implicit default and cannot be specified."
        ),
    )

    @field_validator("text_color")
    @classmethod
    def _reject_explicit_none_color(cls, value: Optional[TextColor]) -> Optional[TextColor]:
        """Reject an explicitly specified ``none`` stroke color.

        ``none`` is the implicit default applied to every line that omits
        ``text_color``; specifying it explicitly is always a user mistake
        (it requests no split while looking like a color), so it is
        rejected wherever the field is accepted.

        Args:
            value: The validated color, or ``None`` (unset).

        Returns:
            The validated color.

        Raises:
            ValueError: If the value is explicitly ``none`` / ``n``.
        """
        if value is TextColor.NONE:
            raise ValueError(
                "text_color 'none' is the implicit default and cannot be "
                "specified explicitly; omit the field instead"
            )
        return value

    @field_validator("font")
    @classmethod
    def _canonicalize_font_name(cls, value: Optional[str]) -> Optional[str]:
        """Canonicalize a requested font name against the font registry.

        Applies to every model inheriting the ``font`` field (TextLine,
        LabelSpec, JobSpec). Matching is case-insensitive; the stored value
        becomes the canonical font name (``plt_fonts.json`` key or TTF
        basename), so downstream consumers never re-match case.

        Args:
            value: The requested font name, or ``None`` (unset).

        Returns:
            The canonical font name, or ``None`` when unset.

        Raises:
            ValueError: If the name matches no known PLT or TrueType font
                (the message lists every valid name).
        """
        if value is None:
            return None
        return normalize_font_name(value)


class LabelAttributes(TextAttributes):
    """Attributes that cascade down to labels.

    Extends TextAttributes with physical label dimensions and layout
    properties. These fields must NOT be inherited by TextLine because
    they describe the label container, not individual glyphs.

    Attributes:
        width: Label width in inches; must be defined at label or job level.
        height: Label height in inches; must be defined at label or job level.
        margin: Optional universal margin in inches (used as fallback for
            h_margin and v_margin when those are unset). Cascades
            label -> job (fallback 0.125).
        h_margin: Optional horizontal margin in inches (left and right edges).
            Cascades label -> job; falls back to ``margin`` if unset.
        v_margin: Optional vertical margin in inches (top and bottom edges).
            Cascades label -> job; falls back to ``margin`` if unset.
        hole_margin: Optional hole margin in inches. The closest point of a
            hole circle to the label edge will be this far from the edge.
            Cascades job -> plate -> label (label overrides plate overrides
            job).
        holes: Optional list of hole specifications. Group locations
            (``corners`` / ``sides``) are expanded in place into their
            atomic member holes at validation time.
        material: Optional stock material name (free-form string, e.g.
            ``"wb"`` or ``"wb(uv)"``). Cascades job -> label (and job ->
            plate, see :class:`PlateSpec`); ``None`` (the default) means
            unset. Labels are grouped by material at packing time: labels
            sharing a material pack together and a plate never mixes
            materials. Comparison is case-insensitive and
            whitespace-trimmed; the first-declared spelling is kept.
    """

    width: Optional[float] = Field(
        default=None,
        gt=0.0,
        description="Label width in inches (must be > 0). Cascades label -> job; must be defined at one level.",
    )
    height: Optional[float] = Field(
        default=None,
        gt=0.0,
        description="Label height in inches (must be > 0). Cascades label -> job; must be defined at one level.",
    )
    margin: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Universal margin in inches (fallback for h_margin and v_margin); "
            "cascades label -> job (fallback 0.125)."
        ),
    )
    h_margin: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Horizontal margin in inches (left and right edges); cascades "
            "label -> job; falls back to margin if unset."
        ),
    )
    v_margin: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Vertical margin in inches (top and bottom edges); cascades "
            "label -> job; falls back to margin if unset."
        ),
    )
    hole_margin: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Distance from hole edge to label edge in inches (label -> job, fallback 0.1875)."
        ),
    )
    holes: Optional[list[HoleSpec]] = Field(
        default=None,
        description=(
            "Drill holes; a label value replaces the job-level list entirely "
            "(an empty list suppresses holes). 'corners'/'sides' expand to members."
        ),
    )
    material: Optional[str] = Field(
        default=None,
        description=(
            "Stock material name (free-form, e.g. 'wb' or 'wb(uv)'); cascades "
            "job -> label. Labels sharing a material pack together and a plate "
            "never mixes materials. Comparison trims whitespace and ignores "
            "case; unset (null) labels pack with the job-level material."
        ),
    )

    @field_validator("material")
    @classmethod
    def _normalize_material(cls, value: Optional[str]) -> Optional[str]:
        """Normalize the cascading ``material`` field.

        Args:
            value: The declared material name, or ``None`` (unset).

        Returns:
            The trimmed material name, or ``None`` when unset.

        Raises:
            ValueError: If the value is whitespace-only.
        """
        return normalize_material(value)

    @field_validator("holes", mode="after")
    @classmethod
    def _expand_hole_location_groups(
        cls, value: Optional[list[HoleSpec]]
    ) -> Optional[list[HoleSpec]]:
        """Expand group hole locations (``corners`` / ``sides``) into members.

        Applies to every model inheriting the ``holes`` field (LabelSpec,
        JobSpec). Group entries are replaced in place by their atomic
        member holes; atomic entries pass through unchanged, so drill
        geometry and ordering semantics for existing specs are untouched.

        Args:
            value: The validated hole list, or ``None`` (unset).

        Returns:
            The expanded hole list (or ``None`` / empty list unchanged).
        """
        if not value:
            return value
        return [hole for spec in value for hole in spec.expand()]


class TextLine(TextAttributes):
    """A single line of text content within a label.

    Attributes:
        text: The actual text string to render.
        text_height: Optional font height in inches. Inherits from parent
            LabelSpec.text_height or JobSpec.text_height if not set locally.
        placeholder: When ``True``, this line is replaced by the corresponding
            item from the replacement file during expansion; all other
            attributes (text height, alignment, color, ...) are preserved on
            the synthesized line. Non-placeholder lines are copied verbatim to
            every generated label instance. Requires the parent label to
            declare ``replacement_text_file``.
    """

    text: str = Field(description="The text string to render (non-empty).")
    placeholder: bool = Field(
        default=False,
        description=(
            "Mark this line for selective replacement. When True, the line's "
            "text is replaced by the corresponding replacement-file item; its "
            "other attributes are preserved. Non-placeholder lines copy verbatim "
            "to every instance. Requires the parent label to declare "
            "replacement_text_file."
        ),
    )


def _validate_replacement_delimiter_value(v: Optional[str]) -> Optional[str]:
    """Validate a replacement-text delimiter (shared by all levels).

    Args:
        v: The configured delimiter, or None (unset).

    Returns:
        The validated delimiter.

    Raises:
        ValueError: If the delimiter is not exactly one character, is a
            newline, or is alphanumeric.
    """
    if v is None:
        return v
    if len(v) != 1:
        raise ValueError("replacement_text_delimiter must be a single character")
    if v in ("\n", "\r"):
        raise ValueError("replacement_text_delimiter cannot be a newline character")
    if v.isalnum():
        raise ValueError(
            "replacement_text_delimiter must be a single special or whitespace character"
        )
    return v


class LabelSpec(LabelAttributes):
    """Specification for a label to be generated.

    Inherits styling fields (text_height, character_spacing,
    line_spacing, width, height, margin, holes) from LabelAttributes.
    ``width`` and ``height`` must be defined either on this label or at
    the job level; they are no longer auto-sized from rendered content.

    A label may be defined in one of two ways:
    1. Statically, with a ``content`` list (and optional ``count``).
    2. As a template driven by an EngraveLab/Vision Pro-style replacement
       text file (``replacement_text_file``). Each line of the file
       produces one label instance; delimited items within a line replace
       the template's text lines. In this mode ``count`` must not be set
       (the file's line count determines it) and ``content`` is optional:
       when omitted, every instance's lines inherit the label-level text
       attributes; when present, its ``text`` values act as placeholders
       declaring the per-line attributes (text height, alignment, etc.).

    Attributes:
        id: Unique identifier for this label specification.
        count: Number of instances to produce. Defaults to 1. Mutually
            exclusive with ``replacement_text_file``.
        content: List of text lines to render on the label. Required unless
            ``replacement_text_file`` is provided.
        replacement_text_file: Path to a replacement text file (resolved
            relative to the job YAML file's directory unless absolute).
            Each line produces one label instance; items within a line are
            separated by ``replacement_text_delimiter``.
        replacement_text_delimiter: Single special or whitespace character
            separating text items within a replacement file line. Defaults
            to ``";"``. Cannot be a newline or an alphanumeric character.
        plate_id: Optional id of the plate this label is pinned to. Pinned
            labels pack exclusively onto that plate, and the plate then
            accepts no other labels. Normally set by plate-level
            ``replacement_text_file`` expansion (see
            :func:`plt_optimizer.generate.substitution.expand_job_spec`);
            it may also be declared directly to pin a static label to a
            specific sheet.
        material: Optional stock material name (inherited from
            :class:`LabelAttributes`). Cascades job -> label; labels
            sharing a material pack together and never share a plate with
            a different material. A plate-level ``replacement_text_file``
            stamps the declaring plate's material onto its synthesized
            labels (see
            :func:`plt_optimizer.generate.substitution.expand_job_spec`).
    """

    id: str = Field(description="Unique identifier for this label specification.")
    count: int = Field(
        ge=1, default=1, description="Number of instances to produce (must be >= 1)."
    )
    content: Optional[list[TextLine]] = Field(
        default=None,
        description=("Text lines to render. Required unless replacement_text_file is provided."),
    )
    replacement_text_file: Optional[str] = Field(
        default=None,
        description=(
            "Path to an EngraveLab/Vision Pro-style replacement text file. "
            "Each line produces one label instance. Mutually exclusive with count."
        ),
    )
    replacement_text_delimiter: Optional[str] = Field(
        default=None,
        description=(
            "Single-character delimiter separating text items within a "
            "replacement file line (default ';')."
        ),
    )
    plate_id: Optional[str] = Field(
        default=None,
        description=(
            "Id of the plate this label is pinned to (packs exclusively "
            "onto that plate). Set by plate-level replacement expansion."
        ),
    )

    @field_validator("replacement_text_delimiter")
    @classmethod
    def _validate_replacement_delimiter(cls, v: Optional[str]) -> Optional[str]:
        """Validate the delimiter (shared rule with job/plate-level fields).

        Args:
            v: The configured delimiter, or None (unset).

        Returns:
            The validated delimiter.

        Raises:
            ValueError: If the delimiter is not exactly one character, is a
                newline, or is alphanumeric.
        """
        return _validate_replacement_delimiter_value(v)

    @model_validator(mode="after")
    def _validate_content_or_replacement(self) -> LabelSpec:
        """Enforce the static-content vs replacement-template contract.

        Raises:
            ValueError: If both ``count`` and ``replacement_text_file`` are
                set, if neither ``content`` nor ``replacement_text_file`` is
                set, if ``content`` is an empty list, or if
                ``replacement_text_delimiter`` is set without a file.

        Returns:
            Self for method chaining.
        """
        if self.replacement_text_file is not None and "count" in self.model_fields_set:
            raise ValueError(
                f"label '{self.id}': 'count' cannot be combined with "
                "'replacement_text_file' (the file's line count determines the count)"
            )
        if self.replacement_text_delimiter is not None and self.replacement_text_file is None:
            raise ValueError(
                f"label '{self.id}': 'replacement_text_delimiter' requires "
                "'replacement_text_file' to be set"
            )
        if self.content is None and self.replacement_text_file is None:
            raise ValueError(
                f"label '{self.id}': must define either 'content' or 'replacement_text_file'"
            )
        if self.content is not None and len(self.content) == 0:
            raise ValueError("content must contain at least one TextLine")
        if self.replacement_text_file is None and self.content is not None:
            bad = [line.text for line in self.content if line.placeholder]
            if bad:
                raise ValueError(
                    f"label '{self.id}': content lines with 'placeholder: true' require "
                    "'replacement_text_file' to be set"
                )
        return self


class PlateSpec(BaseModel):
    """Specification for a plate (material sheet) to cut labels from.

    Attributes:
        id: Unique identifier for this plate specification.
        width: Usable width of the plate in inches (the area available to
            the packer). The material's right edge sits at
            ``left_clearance + width``.
        height: Usable height of the plate in inches (the area available to
            the packer). The material's bottom edge sits at
            ``top_clearance + height``.
        left_clearance: Unused material width along the plate's left edge
            in inches; shifts the usable area rightward. Defaults to 0.0.
            Bottom and right clearances need no fields: the material always
            extends to ``left_clearance + width`` on the right and
            ``top_clearance + height`` at the bottom.
        top_clearance: Unused material height along the plate's top edge in
            inches; shifts the usable area downward (labels stay flush with
            the bottom edge). Defaults to 0.0.
        h_margin: Optional horizontal margin in inches. Accepted for schema
            parity with the job/label ``h_margin`` cascade. NOTE: labels
            are rendered once and cached before bin-packing (and a single
            label may span multiple plates), so a per-plate value is not
            currently applied during rendering; the effective value is
            resolved from the label -> job -> default cascade.
        v_margin: Optional vertical margin in inches. Accepted for schema
            parity with the job/label ``v_margin`` cascade. NOTE: labels
            are rendered once and cached before bin-packing (and a single
            label may span multiple plates), so a per-plate value is not
            currently applied during rendering; the effective value is
            resolved from the label -> job -> default cascade.
        hole_margin: Optional hole margin in inches. Accepted for schema
            parity with the job/label ``hole_margin`` cascade. NOTE: labels
            are rendered once and cached before bin-packing (and a single
            label may span multiple plates), so a per-plate value is not
            currently applied during rendering; the effective value is
            resolved from the label -> job -> default cascade.
        max_h_compress: Optional maximum horizontal compression fraction.
            Accepted for schema parity with the job/label ``max_h_compress``
            cascade. NOTE: labels are rendered once and cached before
            bin-packing (and a single label may span multiple plates), so a
            per-plate value is not currently applied during rendering; the
            effective value is resolved from the label -> job -> default
            cascade.
        text_h_alignment: Optional horizontal text alignment. Accepted for
            schema parity with the job/label ``text_h_alignment`` cascade.
            NOTE: labels are rendered once and cached before bin-packing
            (and a single label may span multiple plates), so a per-plate
            value is not currently applied during rendering; the effective
            value is resolved from the label -> job -> default cascade.
        min_hole_margin: Optional minimum hole margin in inches. Accepted
            for schema parity with the job/label ``min_hole_margin``
            cascade. NOTE: labels are rendered once and cached before
            bin-packing (and a single label may span multiple plates), so
            a per-plate value is not currently applied during rendering;
            the effective value is resolved from the label -> job ->
            default cascade.
        hole_text_collision_distance: Optional minimum engraved-stroke
            air gap in inches. Accepted for schema parity with the
            job/label ``hole_text_collision_distance`` cascade. NOTE:
            labels are rendered once and cached before bin-packing, so a
            per-plate value is not currently applied during rendering;
            the effective value is resolved from the label -> job ->
            default cascade.
        space_width_fraction: Optional space advance fraction. Accepted
            for schema parity with the job/label ``space_width_fraction``
            cascade. NOTE: labels are rendered once and cached before
            bin-packing, so a per-plate value is not currently applied
            during rendering; the effective value is resolved from the
            line -> label -> job -> default cascade.
        min_glyph_width: Optional global minimum glyph advance width in
            inches. Accepted for schema parity with the job/label
            ``min_glyph_width`` cascade. NOTE: labels are rendered once
            and cached before bin-packing, so a per-plate value is not
            currently applied during rendering; the effective value is
            resolved from the line -> label -> job -> default cascade.
        kerning_window_fraction: Optional kerning window fraction in
            ``[0.0, 1.0]``. Accepted for schema parity with the job/label
            ``kerning_window_fraction`` cascade. NOTE: labels are rendered
            once and cached before bin-packing, so a per-plate value is
            not currently applied during rendering; the effective value is
            resolved from the line -> label -> job -> default cascade.
        kerning_penetration_scale: Optional penetration multiplier.
            Accepted for schema parity with the job/label
            ``kerning_penetration_scale`` cascade (not applied at plate
            level).
        kerning_recession_scale: Optional recession multiplier. Accepted
            for schema parity with the job/label
            ``kerning_recession_scale`` cascade (not applied at plate
            level).
        kerning_min_gap: Optional extra kerning air in inches. Accepted
            for schema parity with the job/label ``kerning_min_gap``
            cascade (not applied at plate level).
        fallback_advance_fraction: Optional fallback-advance multiplier.
            Accepted for schema parity with the job/label
            ``fallback_advance_fraction`` cascade (not applied at plate
            level).
        cutter_size: Optional cutter diameter in inches. Accepted for schema
            parity with the job/label ``cutter_size`` cascade (not applied at
            plate level).
        cutter_downsize: Optional compression-driven cutter reduction flag.
            Accepted for schema parity with the job/label ``cutter_downsize``
            cascade (not applied at plate level).
        max_cutter_downsizes: Optional cap on cutter downsizing steps.
            Accepted for schema parity with the job/label
            ``max_cutter_downsizes`` cascade (not applied at plate level).
        layout: Optional per-plate fill-order override (``rows`` /
            ``columns``). ``None`` (the default) inherits the job-level
            ``layout``. Unlike the other cascading fields, this one IS
            applied at packing time: when plates declare different modes,
            same-mode plates pack in one sequential pass and leftovers
            cascade to the next group in declaration order.
        material: Optional stock material name this plate is cut from
            (free-form, e.g. ``"wb"``). Cascades job -> plate (an explicit
            plate value wins; an explicit ``null`` counts as unset). Unlike
            the parity-only fields, this one IS applied at packing time:
            labels are partitioned by material and each material group
            packs only onto plates claiming that material, so a plate
            never mixes materials. A plate that omits material is claimed
            by one material group (material-less plates spread across the
            groups). Comparison trims whitespace and ignores case.
        replacement_text_file: Optional path to an EngraveLab/Vision
            Pro-style replacement text file (resolved relative to the job
            YAML directory unless absolute) whose lines each produce one
            label generated *onto this plate only*. Label dimensions and
            typography (``width`` / ``height`` / ``text_height`` / ...) are
            NOT plate fields: they come from the job-level cascade. The
            plate's usable ``width``/``height`` still describe the pack
            area.
        replacement_text_delimiter: Single special or whitespace character
            separating text items within a replacement file line. Defaults
            to ``";"``. Requires ``replacement_text_file``.
    """

    id: str = Field(description="Unique identifier for this plate specification.")
    width: float = Field(ge=0.0, description="Usable plate width in inches (must be >= 0).")
    height: float = Field(ge=0.0, description="Usable plate height in inches (must be >= 0).")
    left_clearance: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Unused material width along the plate's left edge in inches "
            "(shifts the usable area rightward; must be >= 0)."
        ),
    )
    top_clearance: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Unused material height along the plate's top edge in inches "
            "(shifts the usable area downward; must be >= 0)."
        ),
    )
    h_margin: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Horizontal margin in inches (schema parity; not applied at plate level).",
    )
    v_margin: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Vertical margin in inches (schema parity; not applied at plate level).",
    )
    hole_margin: Optional[float] = Field(
        default=None, ge=0.0, description="Hole margin in inches (must be >= 0)."
    )
    max_h_compress: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Maximum horizontal compression fraction in [0.0, 1.0].",
    )
    text_h_alignment: Optional[TextHAlignment] = Field(
        default=None,
        description="Horizontal text alignment (schema parity; not applied at plate level).",
    )
    font: Optional[str] = Field(
        default=None,
        description=(
            "Font name (schema parity; not applied at plate level). Accepted "
            "and canonicalized like the cascading font field."
        ),
    )
    min_hole_margin: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Minimum hole margin in inches (schema parity; not applied at plate level).",
    )
    space_width_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=("Space advance fraction (schema parity; not applied at plate level)."),
    )
    min_glyph_width: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Global minimum glyph advance width in inches (schema parity; "
            "not applied at plate level)."
        ),
    )
    kerning_window_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Kerning window fraction as a fraction of the rendered text "
            "height (schema parity; not applied at plate level)."
        ),
    )
    kerning_penetration_scale: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Kerning penetration multiplier applied to the detected "
            "penetration (schema parity; not applied at plate level)."
        ),
    )
    kerning_recession_scale: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Kerning recession multiplier applied to a negative (recessed) "
            "detected penetration (schema parity; not applied at plate level)."
        ),
    )
    kerning_min_gap: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Extra kerning air in inches added to every kerned pair advance "
            "(schema parity; not applied at plate level)."
        ),
    )
    fallback_advance_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Fallback advance multiplier on the bounding-box width for pairs "
            "without overlapping height (schema parity; not applied at plate "
            "level)."
        ),
    )
    cutter_size: Optional[float] = Field(
        default=None,
        gt=0.0,
        description=(
            "Cutter diameter in inches overriding automatic selection "
            "(schema parity; not applied at plate level)."
        ),
    )
    cutter_downsize: Optional[bool] = Field(
        default=None,
        description=(
            "Compression-driven cutter reduction permission (schema parity; "
            "not applied at plate level)."
        ),
    )
    max_cutter_downsizes: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Maximum number of cutter downsizing steps (schema parity; not applied at plate level)."
        ),
    )
    hole_text_collision_distance: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Minimum engraved-stroke air gap in inches (schema parity; not applied at plate level)."
        ),
    )
    layout: Optional[LayoutMode] = Field(
        default=None,
        description="Per-plate fill-order override (None = inherit the job layout).",
    )
    material: Optional[str] = Field(
        default=None,
        description=(
            "Stock material this plate is cut from (free-form, e.g. 'wb' or "
            "'wb(uv)'); cascades job -> plate (an explicit plate value wins). "
            "A plate carries exactly one material: labels whose material "
            "matches pack onto it and no other material shares it. A plate "
            "that omits material (null) is claimed by one material group. "
            "Comparison trims whitespace and ignores case."
        ),
    )
    replacement_text_file: Optional[str] = Field(
        default=None,
        description=(
            "Path to an EngraveLab/Vision Pro-style replacement text file. "
            "Each line produces one label packed onto this plate only."
        ),
    )
    replacement_text_delimiter: Optional[str] = Field(
        default=None,
        description=(
            "Single-character delimiter separating text items within a "
            "replacement file line (default ';')."
        ),
    )

    @field_validator("replacement_text_delimiter")
    @classmethod
    def _validate_replacement_delimiter(cls, v: Optional[str]) -> Optional[str]:
        """Validate the delimiter (shared rule with label/job-level fields).

        Args:
            v: The configured delimiter, or None (unset).

        Returns:
            The validated delimiter.

        Raises:
            ValueError: If the delimiter is not exactly one character, is a
                newline, or is alphanumeric.
        """
        return _validate_replacement_delimiter_value(v)

    @field_validator("font")
    @classmethod
    def _canonicalize_font_name(cls, value: Optional[str]) -> Optional[str]:
        """Canonicalize the parity ``font`` field like the cascading one.

        Args:
            value: The requested font name, or ``None`` (unset).

        Returns:
            The canonical font name, or ``None`` when unset.

        Raises:
            ValueError: If the name matches no known PLT or TrueType font.
        """
        if value is None:
            return None
        return normalize_font_name(value)

    @field_validator("material")
    @classmethod
    def _normalize_material(cls, value: Optional[str]) -> Optional[str]:
        """Normalize the plate ``material`` field like the cascading one.

        Args:
            value: The declared material name, or ``None`` (unset).

        Returns:
            The trimmed material name, or ``None`` when unset.

        Raises:
            ValueError: If the value is whitespace-only.
        """
        return normalize_material(value)

    @model_validator(mode="after")
    def _validate_replacement_fields(self) -> PlateSpec:
        """Enforce the plate-level replacement-file contract.

        Raises:
            ValueError: If ``replacement_text_delimiter`` is set without a
                file.

        Returns:
            Self for method chaining.
        """
        if self.replacement_text_delimiter is not None and self.replacement_text_file is None:
            raise ValueError(
                f"plate '{self.id}': 'replacement_text_delimiter' requires "
                "'replacement_text_file' to be set"
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def _drop_null_clearances(cls, data: Any) -> Any:
        """Treat an explicit ``null`` clearance as unset (inherit semantics).

        Mirrors the job-config convention that an explicit YAML ``null``
        counts as unset: dropping the key lets the plate fall through to
        the job-level ``left_clearance`` / ``top_clearance`` cascade (see
        :meth:`JobSpec._apply_job_level_clearances`) or its own ``0.0``
        default. The same rule applies to ``material``: an explicit
        ``null`` falls through to the job-level cascade (see
        :meth:`JobSpec._apply_job_level_materials`) or stays unset.

        Args:
            data: Raw input mapping (or any other input, passed through).

        Returns:
            The mapping with null clearance/material keys removed.
        """
        if isinstance(data, dict):
            for key in ("left_clearance", "top_clearance", "material"):
                if data.get(key) is None:
                    data = {k: v for k, v in data.items() if k != key}
        return data


class JobSpec(LabelAttributes):
    """Top-level specification for a batch label generation job.

    Inherits styling fields from LabelAttributes so they can be set at the
    job level and inherited down to labels and text lines. ``width`` and
    ``height`` must be defined at either the job level or on each label.

    A job may be specified in one of three forms:
    1. A list of explicit labels (`labels`), each with width/height defined.
    2. A single root-level label definition (`content` + optional `count`),
       with width/height defined at the job or root level.
    3. A job-level EngraveLab/Vision Pro-style replacement text file
       (``replacement_text_file``): each line of the file produces one
       label, generated from the job-level label attributes. ``width``,
       ``height`` and ``text_height`` must be defined at the job level;
       ``content`` is an optional per-line attribute template (exactly
       like on :class:`LabelSpec`). This form is mutually exclusive with
       ``labels`` and ``count``.

    The label source forms (1-3) are mutually exclusive; exactly one must
    be provided. The ``width`` and ``height`` cascade from label to job;
    they are no longer auto-sized from rendered content.

    Attributes:
        job_name: Human-readable name for this job.
        plates: Optional list of plate specifications. If omitted, the
            generation pipeline auto-allocates default 24x16 sheets.
        labels: Optional list of unique label specifications to produce.
        count: Optional count for root-level single-label jobs.
        content: Optional root-level content for single-label jobs (or the
            per-line attribute template for a job-level replacement file).
        replacement_text_file: Job-level replacement text file (resolved
            relative to the job YAML directory unless absolute). Each line
            produces one label; items within a line are separated by
            ``replacement_text_delimiter``. Requires job-level ``width``,
            ``height`` and ``text_height``; mutually exclusive with
            ``labels``, ``count`` and plate-level replacement files.
        replacement_text_delimiter: Single special or whitespace character
            separating text items within a replacement file line. Defaults
            to ``";"``. Requires ``replacement_text_file``.
        allow_rotation: Whether the bin packer may rotate label instances
            90 degrees to improve plate utilization. When a rotated label
            is assembled onto a plate its whole content (text, border and
            drill holes) is rotated clockwise. Defaults to True.
        text_chunk_mode: Granularity at which rendered text becomes a
            plate-space optimization node: ``"line"`` (the default) routes
            each whole text line as one unit; ``"word"`` splits lines on
            whitespace for finer rapid-travel routing at the cost of more
            TSP nodes.
        layout: Preferential plate fill order for bin packing. ``columns``
            (the default) fills each plate's height before extending
            rightward; ``rows`` fills width before extending downward (the
            historical behaviour). Cascades job -> plate: a plate may
            override it via ``PlateSpec.layout``.
        left_clearance: Job-level default for the plate left-edge
            clearance in inches. Cascades job -> plate: plates that omit
            ``left_clearance`` inherit this value; an explicit plate
            value (including ``0.0``) always wins. Unbounded
            auto-allocated sheets use it too (threaded through the export
            call). ``None`` (the default) means no job-level default;
            plates then fall back to their own ``0.0`` default.
        top_clearance: Job-level default for the plate top-edge clearance
            in inches (same cascade as ``left_clearance``).
    """

    job_name: str = Field(description="Human-readable name for this job.")
    plates: Optional[list[PlateSpec]] = Field(
        default=None,
        description=(
            "Plate (material sheet) definitions. Omit for unbounded mode: "
            "auto-allocated default_plate_{i} sheets (24x16 unless job-config "
            "plate_width/plate_height override)."
        ),
    )

    # Allow either a list of labels, or a root-level label definition
    labels: Optional[list[LabelSpec]] = Field(
        default=None,
        description=(
            "Explicit label list. Mutually exclusive with root-level content "
            "and a job-level replacement_text_file."
        ),
    )
    count: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Instance count for root-level single-label jobs; not allowed with replacement files."
        ),
    )
    content: Optional[list[TextLine]] = Field(
        default=None,
        description=(
            "Root-level text lines (single-label job) or the per-line "
            "attribute template when a replacement file is in play."
        ),
    )
    replacement_text_file: Optional[str] = Field(
        default=None,
        description=(
            "Job-level EngraveLab/Vision Pro-style replacement text file. "
            "Each line produces one label built from the job-level label "
            "attributes. Mutually exclusive with labels and count."
        ),
    )
    replacement_text_delimiter: Optional[str] = Field(
        default=None,
        description=(
            "Single-character delimiter separating text items within a "
            "replacement file line (default ';')."
        ),
    )

    allow_rotation: bool = Field(
        default=True,
        description=(
            "Allow the bin packer to rotate labels 90 degrees (clockwise at "
            "assembly) for tighter layouts. Set false to keep every label "
            "horizontal."
        ),
    )
    text_chunk_mode: Literal["line", "word"] = Field(
        default="line",
        description=(
            "Plate-space text optimization granularity: 'line' routes each "
            "text line as one node (default); 'word' routes each "
            "whitespace-delimited word separately."
        ),
    )
    layout: LayoutMode = Field(
        default=DEFAULT_LAYOUT_MODE,
        description=(
            "Preferential plate fill order: 'columns' (default) fills the "
            "plate height before extending rightward; 'rows' fills width "
            "before extending downward."
        ),
    )
    left_clearance: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Job-level default plate left-edge clearance in inches "
            "(cascades job -> plate; an explicit plate value, including "
            "0.0, wins)."
        ),
    )
    top_clearance: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Job-level default plate top-edge clearance in inches "
            "(cascades job -> plate; an explicit plate value, including "
            "0.0, wins)."
        ),
    )
    auto_line_spacing_interline_to_top_bottom_ratio: Optional[float] = Field(
        default=None,
        gt=0.0,
        description=(
            "Ratio controlling inter-line spacing relative to top/bottom margins "
            "when auto line spacing is enabled. Default 1.0 makes all gaps equal. "
            "Values > 1.0 increase inter-line spacing at the expense of top/bottom margins."
        ),
    )
    tool_options: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional engraver tool options (header commands). Keys are underscored "
            "header names (e.g., 'cutting_velocity', 'dwell_time', 'spindle_speed'). "
            "Values override job-config.json defaults for those parameters. "
            "Supported options and their ranges are defined in job-config.json "
            "'tool_options' metadata, which includes 'command', 'type', 'units', "
            "'default' (with dual text/borders_holes values), and for numeric types "
            "'min'/'max' bounds. Omitted options use job-config defaults or are "
            "omitted from the PLT if the default is null. Out-of-bounds values are "
            "clamped to the configured range with a WARNING logged."
        ),
    )

    @field_validator("replacement_text_delimiter")
    @classmethod
    def _validate_replacement_delimiter(cls, v: Optional[str]) -> Optional[str]:
        """Validate the delimiter (shared rule with label/plate-level fields).

        Args:
            v: The configured delimiter, or None (unset).

        Returns:
            The validated delimiter.

        Raises:
            ValueError: If the delimiter is not exactly one character, is a
                newline, or is alphanumeric.
        """
        return _validate_replacement_delimiter_value(v)

    @model_validator(mode="after")
    def _apply_job_level_clearances(self) -> JobSpec:
        """Cascade job-level edge clearances onto plates that omit them.

        Mirrors the ``layout`` job -> plate cascade: an explicit plate
        value (including an explicit ``0.0``) always wins; plates that
        omit a clearance inherit the job-level value when set. Without a
        job-level value, plates keep their own default (``0.0``), so
        existing specs are unaffected.

        Returns:
            Self for method chaining.
        """
        if self.plates is None:
            return self
        updated_plates: list[PlateSpec] = []
        for plate in self.plates:
            fill: dict[str, float] = {}
            if self.left_clearance is not None and "left_clearance" not in plate.model_fields_set:
                fill["left_clearance"] = self.left_clearance
            if self.top_clearance is not None and "top_clearance" not in plate.model_fields_set:
                fill["top_clearance"] = self.top_clearance
            updated_plates.append(plate.model_copy(update=fill) if fill else plate)
        self.plates = updated_plates
        return self

    @model_validator(mode="after")
    def _apply_job_level_materials(self) -> JobSpec:
        """Cascade the job-level ``material`` onto plates that omit it.

        Mirrors :meth:`_apply_job_level_clearances`: an explicit plate
        value always wins (an explicit ``null`` counts as unset, see
        :meth:`PlateSpec._drop_null_clearances`); plates that omit
        ``material`` inherit the job-level value when set. Without a
        job-level value, plates keep ``None`` (material-agnostic), so
        existing specs are unaffected.

        Returns:
            Self for method chaining.
        """
        if self.plates is None or self.material is None:
            return self
        updated_plates: list[PlateSpec] = []
        for plate in self.plates:
            if "material" not in plate.model_fields_set:
                updated_plates.append(plate.model_copy(update={"material": self.material}))
            else:
                updated_plates.append(plate)
        self.plates = updated_plates
        return self

    @model_validator(mode="after")
    def _reject_job_level_text_color(self) -> JobSpec:
        """Reject a job-level ``text_color`` declaration.

        ``text_color`` exists solely to distinguish otherwise-equivalent
        text within a label or between labels; a job-wide color would
        separate nothing (that is exactly what the implicit ``none``
        default means), so the field is accepted on labels and text lines
        only -- including root-level single-label jobs, where every other
        label attribute is set at the job level.

        Raises:
            ValueError: Always, when ``text_color`` is set at the job level.

        Returns:
            Self for method chaining.
        """
        if self.text_color is not None:
            raise ValueError(
                "'text_color' cannot be set at the job level; declare it on "
                "a label or an individual text line to split otherwise-"
                "identical text into separate toolpaths"
            )
        return self

    @model_validator(mode="after")
    def validate_job_structure(self) -> JobSpec:
        """Ensure exactly one label source form is provided.

        The label-source forms are ``labels``, root-level ``content``, a
        job-level ``replacement_text_file``, and plate-level replacement
        files (which synthesize their own labels). With a job-level
        replacement file, ``content`` is an optional per-line attribute
        template (not a second label source), mirroring :class:`LabelSpec`;
        the same holds for a plate-level file.

        Raises:
            ValueError: If no label source is defined, or an explicit
                ``labels``/``content`` list is combined with a job-level
                replacement file.

        Returns:
            Self for method chaining.
        """
        has_labels = self.labels is not None and len(self.labels) > 0
        has_replacement = self.replacement_text_file is not None
        has_plate_files = any(
            plate.replacement_text_file is not None for plate in self.plates or []
        )
        # Root-level content counts as a label source only when it is not
        # serving as a replacement-file attribute template (job- or
        # plate-level).
        is_template = has_replacement or (has_plate_files and self.labels is None)
        has_content = (not is_template) and self.content is not None and len(self.content) > 0

        forms = sum(
            1 for present in (has_labels, has_content, has_replacement, has_plate_files) if present
        )
        if forms == 0:
            raise ValueError(
                "Job must define 'labels', root-level 'content', or a "
                "'replacement_text_file' (job- or plate-level)."
            )
        if has_labels and (has_replacement or has_content):
            raise ValueError(
                "Job cannot define more than one of 'labels', root-level 'content' "
                "and 'replacement_text_file'."
            )

        return self

    @model_validator(mode="after")
    def _validate_replacement_structure(self) -> JobSpec:
        """Enforce the job-level and plate-level replacement-file contracts.

        A job-level replacement file synthesizes its labels from the
        job-level attributes, so it requires job-level ``width``, ``height``
        and ``text_height`` (and no ``count``). Plate-level replacement
        files pin their generated labels to the declaring plate; the
        synthesized labels likewise carry no label-level dimensions, so the
        same job-level attributes are required.

        Raises:
            ValueError: If a delimiter is set without a file, ``count`` is
                combined with a job-level file (or with plate-level files
                in root-content mode), a job-level file is combined with
                plate-level files, or a replacement file (job- or
                plate-level) is declared without the required job-level
                label attributes.

        Returns:
            Self for method chaining.
        """
        if self.replacement_text_delimiter is not None and self.replacement_text_file is None:
            raise ValueError(
                "'replacement_text_delimiter' requires 'replacement_text_file' to be set"
            )
        if self.replacement_text_file is not None and "count" in self.model_fields_set:
            raise ValueError(
                "'count' cannot be combined with 'replacement_text_file' "
                "(the file's line count determines the label count)"
            )

        plate_replacement_ids = [
            plate.id for plate in self.plates or [] if plate.replacement_text_file is not None
        ]
        if self.replacement_text_file is not None and plate_replacement_ids:
            raise ValueError(
                "job-level 'replacement_text_file' cannot be combined with "
                f"plate-level replacement files (plate(s): {', '.join(plate_replacement_ids)})"
            )
        if plate_replacement_ids and "count" in self.model_fields_set and self.labels is None:
            raise ValueError(
                "'count' cannot be combined with plate-level 'replacement_text_file' "
                "(the file's line count determines each plate's label count)"
            )

        if self.replacement_text_file is not None or plate_replacement_ids:
            missing = [
                name for name in ("width", "height", "text_height") if getattr(self, name) is None
            ]
            if missing:
                scope = (
                    "job-level 'replacement_text_file'"
                    if self.replacement_text_file is not None
                    else "plate-level 'replacement_text_file' "
                    f"(plate(s): {', '.join(plate_replacement_ids)})"
                )
                raise ValueError(
                    f"{scope} synthesizes labels from the job-level attributes; "
                    f"job-level {', '.join(missing)} must be defined"
                )

        return self

    @model_validator(mode="after")
    def _validate_label_plate_references(self) -> JobSpec:
        """Validate ``LabelSpec.plate_id`` references against the plate list.

        Raises:
            ValueError: If a label pins itself to a plate id that is not
                declared (or no plates are declared at all).

        Returns:
            Self for method chaining.
        """
        if not self.labels:
            return self
        known_ids = {plate.id for plate in self.plates or []}
        for label in self.labels:
            if label.plate_id is not None and label.plate_id not in known_ids:
                raise ValueError(
                    f"label '{label.id}': plate_id '{label.plate_id}' does not "
                    "reference a declared plate"
                )
        return self

    @model_validator(mode="after")
    def _validate_width_height_defined(self) -> JobSpec:
        """Ensure width and height are defined at label or job level.

        Since auto-sizing from rendered content is no longer supported,
        every label (static or synthesized) must have width and height
        defined either explicitly at the label level or cascaded from
        the job level. This validator checks that requirement.

        Raises:
            ValueError: If a label lacks both width and height and the job
                lacks width/height to cascade; if root-level labels lack
                dimensions and the job doesn't provide them; or if a
                job-level or plate-level replacement file doesn't have
                job-level width/height (already checked in
                _validate_replacement_structure, but enforced here too for
                consistency).

        Returns:
            Self for method chaining.
        """
        job_width = self.width
        job_height = self.height

        # Check explicit labels (if present)
        if self.labels:
            for label in self.labels:
                label_width = label.width or job_width
                label_height = label.height or job_height
                if label_width is None or label_height is None:
                    missing = []
                    if label_width is None:
                        missing.append("width")
                    if label_height is None:
                        missing.append("height")
                    raise ValueError(
                        f"label '{label.id}': {', '.join(missing)} must be defined "
                        f"at the label level or inherited from the job level"
                    )

        # Check root-level content (if present as a label source, not as a template)
        if self.content is not None and len(self.content) > 0:
            # Only check if content is a label source (not a replacement template)
            has_replacement = self.replacement_text_file is not None
            has_plate_files = any(
                plate.replacement_text_file is not None for plate in self.plates or []
            )
            is_template = has_replacement or (has_plate_files and self.labels is None)

            if not is_template:
                # Root-level content is a label source
                if job_width is None or job_height is None:
                    missing = []
                    if job_width is None:
                        missing.append("width")
                    if job_height is None:
                        missing.append("height")
                    raise ValueError(
                        f"Root-level job with 'content': {', '.join(missing)} must be "
                        f"defined at the job level"
                    )

        return self


def parse_yaml(
    file_path: str | Path,
    job_config_path: str | Path | None = None,
) -> JobSpec:
    """Parse and validate a YAML job specification file.

    Args:
        file_path: Path to the YAML specification file.
        job_config_path: Optional path to a ``job-config.json`` file (see
            :mod:`plt_optimizer.generate.job_config`). When provided (and
            the file exists), its defaults are injected at the top-most
            (job/plate) layer before validation, and the
            required-when-unconfigured fields are enforced. ``None``
            (the default) keeps the historical all-optional contract.

    Returns:
        A validated JobSpec instance with all nested models populated.

    Raises:
        FileNotFoundError: If the specified file does not exist.
        ValueError: If the YAML content is invalid or fails validation, or
            if a required field is missing from both the job config and
            the spec (``JobConfigError``, a ``ValueError`` subclass).
        yaml.YAMLError: For malformed YAML syntax.

    Example:
        >>> job = parse_yaml("tests_deps/sample_spec.yaml")
        >>> print(f"Loaded {job.job_name}")
    """
    # Imported lazily: job_config imports schema, so a module-level import
    # would be circular.
    from plt_optimizer.generate.job_config import (
        apply_job_config_defaults,
        assert_required_fields,
        load_job_config,
    )

    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"Specification file not found: {path}")

    with open(path, encoding="utf-8") as f:
        raw_data = yaml.safe_load(f)

    # Validate top-level structure
    if raw_data is None:
        raise ValueError("Empty YAML document")

    job_data = raw_data.get("job")
    if job_data is None:
        raise ValueError("Missing 'job' root element")

    config = load_job_config(Path(job_config_path) if job_config_path is not None else None)
    job_data = apply_job_config_defaults(job_data, config)
    assert_required_fields(job_data, config)

    return JobSpec(**job_data)
