"""Inline HTML-like markup parsing for label text lines.

This module is the **framework** for the inline markup syntax described in
``plt_optimizer/generate/markup/AGENTS.md`` ("Inline Markup & Italic"). It
splits a text line's raw ``text`` into :class:`Run` s of (plain text,
:class:`Style`), so each run can be rendered with its own typographic
transform (the first markup being ``<i>...</i>``, italicization).

Design guarantees (mirroring ``descender_adjust.py``):

* **Pure Python** -- matplotlib-free, numpy-free, Python 3.8 compatible
  (Windows 7 import guarantee). No pipeline imports at all.
* **Configurable delimiters** -- the bracket pair comes from the cascading
  ``html_brackets`` field: an even-length string whose first half is the left
  delimiter and second half the right delimiter (default ``"<>"`` -> ``<``
  and ``>``). Multi-character pairs such as ``"«»"`` or ``"<<>>"`` are legal.
* **Literal-first scanning** -- the leftmost left delimiter is scanned; the
  enclosed body is a tag *only* when it matches ``[A-Za-z]+`` (open, with the
  reserved ``name:args`` form) or ``/[A-Za-z]+`` (close) and names a
  registered tag. Everything else -- a lone ``<`` in ``3 < 5``, a chevron, an
  unregistered ``<xyz>`` -- is literal text. The forward slash is special
  *only* inside the delimiters (``</i>``), never elsewhere.
* **Crossing spans** -- a close tag closes the most recent open tag with
  that name, which need not be the innermost one: ``<s>Always <sc>vacuum
  </s> charging line</sc>`` is legal and engraves the per-character
  *intersection* of the spans (HTML-style) -- ``Always `` struck, ``vacuum``
  struck + small caps, `` charging line`` small caps. The close removes its
  opener from the stack and rebuilds the active style by replaying the
  remaining open tags (each tag's ``apply`` is idempotent, so a crossing
  close restores the surrounding spans exactly).
* **Hard errors on broken markup** -- a close tag whose name was never
  opened, a close tag with no open tag at all, or an open tag never closed
  raises :class:`MarkupError` (a ``ValueError`` subclass) so the job aborts
  loudly, consistent with the repo's fail-loud philosophy (unknown font,
  missing glyph).

Tag registry
------------
Markup types are registered in :data:`TAG_REGISTRY` as :class:`TagSpec`
entries (name + an ``apply(style, arg)`` mapping + an ``accepts_args`` flag
for the ``name:args`` syntax, e.g. ``<scale:0.5>``). The ``<f:font name>``
font tag is the one tag whose argument needs pipeline knowledge (the font
catalog): :func:`parse_markup` accepts an injected ``font_resolver``
(raw name -> canonical name, ``ValueError`` for unknown) that canonicalizes
``Style.font`` at open time and on every crossing-close replay, so this
module stays free of pipeline imports (Windows 7 / Python 3.8 guarantee). A tag that accepts args
validates its argument inside ``apply`` (raising :class:`MarkupError` for a
bad value) and supplies an ``arg(style)`` serializer so
:func:`serialize_runs` re-emits the value (the line-content reflow
round-trips raw text through the serializer, so an arg-unaware serializer
would silently drop it). An ``args_optional`` tag (``<sup>``, ``<sub>``)
makes the ``name:args`` argument optional: the bare ``<name>`` IS markup
(``apply`` receives ``""`` and derives its value) and the canonical
serializer returns ``""`` for the bare form so serialization stays
round-trip exact. Adding a markup type (``<b>``, ``<u>``, ...) is a
one-line registry entry plus the render-side handling of the new
:class:`Style` field; the parser, cascade, and serialization need no change.

The shipped tags are ``<i> <u> <b> <scale:N> <sup> <sub> <s> <sc> <f:name>``,
``<c:D>`` (the explicit cutter diameter in inches for the wrapped segment),
``<oc>`` (the original cutter: the wrapped segment keeps the line's own
tool, immune to markup-induced cutter changes), ``<vc>`` (vertical
centering: the wrapped segment's glyphs are raised or lowered so each glyph's
ink is centred between the baseline and the capline), ``<tc:color>`` (the
stroke-color layer of the wrapped segment -- the inline form of the
``text_color`` field; the name is canonicalized by the ``color_resolver``
injected into :func:`parse_markup`) and ``<cb:D>`` (the kerf buffer in inches
for the wrapped segment -- overrides the line's cascaded ``cutter_buffer_text``
per character; ``0.0`` explicitly zeroes the buffer for the span).

Manual kerning
--------------
The cascading ``kern_pos_string`` / ``kern_neg_string`` fields add
**marker strings** (not tags -- there is no open/close form): every
occurrence of the positive marker in the raw text is stripped and replaced
by a forward cursor shift, every occurrence of the negative marker by a
backward one (the width is the cascading ``kern_width_fraction`` of the cap
height, applied by the pipeline). ``parse_markup`` accepts the two strings
and performs the stripping after the tag scan, so a marker only matches in
**literal** text -- ``<->`` is a legal marker (the body ``-`` is not a
registered tag, so it survives the scan as literal), while ``<s>`` is not
(the strikethrough tag consumes it first; the schema validator rejects a
marker that *is* markup). The count attaches to the character the marker
precedes (:attr:`Style.kern`, a signed int so adjacent markers accumulate),
which keeps ``plain_text`` marker-free and every per-character array
aligned with it. A trailing marker (after the last character) has no
following character and is dropped; a leading marker shifts the line's
first glyph (the line's ink box normalizes the whole-line translate).
:func:`serialize_runs` re-emits the markers before the flagged run's text,
so the line-content reflow round-trips them through the raw text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Pattern, Sequence, Tuple

__all__ = [
    "MAX_BUFFER_ARG",
    "MAX_CUTTER_ARG",
    "MAX_SCALE_ARG",
    "MAX_SHIFT_ARG",
    "MIN_BUFFER_ARG",
    "MIN_CUTTER_ARG",
    "MIN_SCALE_ARG",
    "MIN_SHIFT_ARG",
    "MarkupError",
    "PLAIN",
    "Piece",
    "Run",
    "SUB_DIRECTION",
    "SUP_DIRECTION",
    "StyledText",
    "StyledWord",
    "Style",
    "SupSub",
    "TAG_REGISTRY",
    "TagSpec",
    "UNSET_SCALE",
    "parse_markup",
    "register_tag",
    "serialize_runs",
    "serialize_word",
    "split_brackets",
    "split_styled_words",
    "split_styled_words_with_separators",
    "style_boundaries",
]


class MarkupError(ValueError):
    """Raised when inline markup is malformed (mismatched/unclosed tag).

    Subclass of :class:`ValueError` so the CLI surfaces the abort as a
    non-zero exit, like the other spec-level validation errors.
    """


@dataclass(frozen=True)
class SupSub:
    """The ``<sup>``/``<sub>`` baseline shift active on a markup run.

    Frozen (hashable, value-equal) so :func:`serialize_runs` can recover the
    tag stack from styles like every other :class:`Style` field.

    Attributes:
        direction: ``+1`` (:data:`SUP_DIRECTION`) for a superscript run,
            ``-1`` (:data:`SUB_DIRECTION`) for a subscript run. The tag name
            fixes the direction, so the two tags are mutually exclusive
            (each ``is_active`` reads its own sign) and nesting one inside
            the other composes like the other value tags.
        shift: The explicit baseline shift in cap-height units (``0`` = the
            current baseline, ``1`` = the capline, ``-1`` = one cap height
            below), from the tag's optional argument (``<sup:0.3>``).
            ``None`` marks the bare ``<sup>``/``<sub>`` form: the pipeline
            derives the shift from the cascading
            ``supersubscript_downscale`` ``d``, centering the shrunken
            glyph on the line it belongs to (superscript ``1 - d / 2``,
            subscript ``-d / 2``), so the downscaled glyph's centre lands
            exactly on the capline (sup) / baseline (sub).
    """

    direction: int
    shift: Optional[float] = None


@dataclass(frozen=True)
class Style:
    """Immutable typographic style carried by a markup run.

    Attributes:
        italic: True when the run is wrapped in a registered italic tag
            (``<i>`` with the default brackets). Renderers shear PLT glyphs
            (or use a native TTF italic) for italic runs.
        underline: True when the run is wrapped in a registered underline tag
            (``<u>`` with the default brackets). Renderers draw a centerline
            underline (the line's own cutter) spanning each maximal
            underlined run's ink width, gap-cut around glyphs whose ink
            descends into the underline's clearance.
        bold: True when the run is wrapped in a registered bold tag (``<b>``
            with the default brackets). The run's glyphs engrave with the
            line's thicker **bold cutter** (the next size up in the
            inventory, or the multi-stroke boldness ladder at the boosted
            factor), scaled uniformly about the baseline so the engraved
            stroke width stays inside the glyph.
        scale: The ``<scale:N>`` factor active on the run (``1.0`` = the
            identity when the run carries no scale tag). The run's glyphs
            engrave an engraved silhouette exactly ``scale`` times the plain
            run's with the same cutter (the centerline contract: the glyph
            geometry scales by ``(scale * nominal - cutter) /
            (nominal - cutter)`` uniformly about the baseline, so ``AA``
            arcs stay native).
        sup_sub: The ``<sup>``/``<sub>`` baseline shift active on the run
            (``None`` = unmarked). The run's glyphs engrave shifted
            vertically by :attr:`SupSub.shift` cap-height units (the bare
            tag derives the shift -- and a uniform downscale by the
            cascading ``supersubscript_downscale`` -- in the pipeline),
            composed multiplicatively with :attr:`scale`.
        strikethrough: True when the run is wrapped in a registered
            strikethrough tag (``<s>`` with the default brackets). Renderers
            draw ONE continuous centerline stroke (the run's own cutter)
            spanning each maximal struck run's ink width at the line's
            strikethrough height -- **no** gap cuts, so a descender punches
            through the stroke instead of breaking it (the underline's
            minimal-break model deliberately does not apply here).
        small_caps: True when the run is wrapped in a registered small-caps
            tag (``<sc>`` with the default brackets). The run's ASCII
            lowercase ``a-z`` engrave as the base font's **uppercase** glyph
            uniformly shrunk to ``sc_scale`` of the cap height, top-down (the
            glyph's own bottom stays in place) -- the exact transform the
            ``<base> SC`` font variant applies to the whole line, selected
            per character. Caseless characters (uppercase, digits,
            punctuation, derived symbols) engrave verbatim at full size, the
            character map stays 1:1, and the nominal/toolpath ``text_height``
            is untouched (no cutter change). Inert on a line whose font is
            already a small-caps variant (the run is *already* in small caps,
            so the tag has no additional effect) and on a symbol font
            (the catalog's ``symbol_font`` flag), which has no lowercase to shrink.
        font: The ``<f:font name>`` font selection active on the run
            (``None`` = the line's own cascaded ``font``). The run's
            characters engrave with the named font -- the same names the
            ``font`` field accepts (PLT catalog key, TTF basename, or a
            ``<base> SC`` variant; case-insensitive, canonicalized by the
            ``font_resolver`` injected into :func:`parse_markup`) -- at the
            line's ``text_height`` and with the cutter that font's own
            ladder gives that height (the ``<scale:N>`` contract). The tag
            argument regex is ``\\S*`` (no whitespace), so ``+`` is the
            canonical space in a multi-word name (``<f:Foo+Bar>``); ``_``
            stays literal. PLT and TTF families mix freely inside one line.
        cutter: The ``<c:0.06>`` explicit cutter diameter (inches) active
            on the run (``None`` = the line's own cascaded cutter). The
            wrapped characters engrave with THIS tool, overriding every
            other cutter setting for the segment (the line's own tool, the
            ``<b>`` bold rung, the ``<scale:N>`` height ladder). The
            argument is in inches (like every other cutter value in the
            repo) and must be positive; the pipeline snaps it to the shop
            inventory and validates it against the line's text height.
        original_cutter: True when the run is wrapped in a registered
            original-cutter tag (``<oc>`` with the default brackets). The
            wrapped characters engrave with the **line's own plain cutter**
            -- the tool unmarked text on the line gets -- and are therefore
            immune to every *markup-induced* cutter change: the
            ``<scale:N>`` height ladder, the ``<b>`` bold rung, the bare
            ``<sup>``/``<sub>`` downscale ladder and the ``<f:...>`` run
            ladder all leave the segment on the line's tool. The line's own
            cutter may still move on its own (the compression-driven
            downsize), and the pinned segment follows it. An explicit
            ``<c:D>`` tool still wins over this tag (an explicit tool is a
            human decision, never overridden automatically).
        v_center: True when the run is wrapped in a registered vertical-
            centering tag (``<vc>`` with the default brackets). The wrapped
            characters engrave **raised or lowered** so each glyph's own ink
            is vertically centred between the line's baseline and capline
            (the cap-band midline ``toolpath_text_height / 2`` above the
            baseline) -- a pure vertical translate riding the renderers'
            ``baseline_shifts`` array (the ``<sup>``/``<sub>`` plumbing),
            composed with the existing shift and the engraved-baseline
            raise. The pipeline also auto-applies the same centering to
            every character of the cascading ``v_center_chars`` set (the
            hyphen, colon, en/em dash, ``=`` ...), so the tag is the manual
            override for any other character. Applying it to an already
            centred run is the identity (a flag tag).
        kern: The signed **manual-kern marker count** attached to the run's
            first character (``0`` = unmarked). One ``kern_pos_string``
            occurrence adds ``+1``, one ``kern_neg_string`` adds ``-1``, and
            adjacent markers accumulate (``<+><+>`` = ``+2``). The marker
            strings never engrave -- ``parse_markup`` strips them from the
            raw text and attaches the count here, so the pipeline converts
            it into a cursor shift of ``kern * kern_width_fraction * cap
            height`` inches before the run's first glyph. The count rides
            the style (never a stored positional array), so a pre-pass that
            rewrites the line's text (``with_markup``, the line-content
            reflow) keeps every marker on its own character.
        text_color: The ``<tc:color>`` stroke-color layer active on the run
            (``None`` = the line's own cascaded ``text_color``). The run's
            characters engrave on the named color's toolpath layer -- the
            same names the ``text_color`` field accepts (full name or
            1-letter abbreviation, case-insensitive, canonicalized to the
            full name by the ``color_resolver`` injected into
            :func:`parse_markup`) -- splitting otherwise-identical text into
            a separate HPGL ``SP`` layer and per-cutter PLT file (the depth
            changes between runs on 3-layer stock). A pure layer tag: no
            geometry, cutter, or height change.
    """

    italic: bool = False
    underline: bool = False
    bold: bool = False
    scale: float = 1.0
    sup_sub: Optional[SupSub] = None
    strikethrough: bool = False
    small_caps: bool = False
    font: Optional[str] = None
    cutter: Optional[float] = None
    original_cutter: bool = False
    v_center: bool = False
    kern: int = 0
    text_color: Optional[str] = None
    cutter_buffer: Optional[float] = None


#: The style of unmarked text (every field at its default).
PLAIN = Style()

#: Lower bound of the ``<scale:N>`` argument (matches the repo's downscale
#: fields: ``sc_scale``, ``descending_character_v_scale``).
MIN_SCALE_ARG: float = 0.2

#: Upper bound of the ``<scale:N>`` argument (bounds runaway specs).
MAX_SCALE_ARG: float = 4.0

#: The identity scale -- :attr:`Style.scale`'s default, "no scale tag".
UNSET_SCALE: float = 1.0

#: Lower bound of the ``<sup:N>``/``<sub:N>`` shift argument, in cap-height
#: units (``-1`` = one cap height below the baseline).
MIN_SHIFT_ARG: float = -1.0

#: Upper bound of the ``<sup:N>``/``<sub:N>`` shift argument, in cap-height
#: units (``1`` = the capline).
MAX_SHIFT_ARG: float = 1.0

#: :attr:`SupSub.direction` for a superscript run (``<sup>``).
SUP_DIRECTION: int = 1

#: :attr:`SupSub.direction` for a subscript run (``<sub>``).
SUB_DIRECTION: int = -1

#: Lower bound of the ``<c:D>`` argument (exclusive -- a cutter must be
#: positive). The real limit is the line's own text height, which the
#: pipeline checks at resolution time (a ``CutterSizeError``).
MIN_CUTTER_ARG: float = 0.0

#: Upper bound of the ``<c:D>`` argument (inches). Generous: the largest
#: tool in the shop inventory is 0.25in, so 1.0in only bounds a typo.
MAX_CUTTER_ARG: float = 1.0

#: Lower bound of the ``<cb:D>`` argument (inclusive -- 0.0 explicitly
#: overrides the line's buffer to zero, which is a valid "no extra buffer").
MIN_BUFFER_ARG: float = 0.0

#: Upper bound of the ``<cb:D>`` argument (inches). Generous: the same
#: ceiling as ``MAX_CUTTER_ARG`` bounds runaway specs.
MAX_BUFFER_ARG: float = 1.0


def _parse_scale_arg(arg: str) -> float:
    """Parse and validate a ``<scale:N>`` argument.

    Args:
        arg: The raw ``name:args`` argument text.

    Returns:
        The scale factor.

    Raises:
        MarkupError: If the argument is not a float in
            ``[MIN_SCALE_ARG, MAX_SCALE_ARG]`` (fail-loud, like an
            unclosed tag).
    """
    try:
        scale = float(arg)
    except ValueError:
        raise MarkupError(
            f"scale tag argument {arg!r} is not a number (expected e.g. <scale:0.5>)."
        ) from None
    if not MIN_SCALE_ARG <= scale <= MAX_SCALE_ARG:
        raise MarkupError(
            f"scale tag argument {arg!r} is outside the supported range "
            f"[{MIN_SCALE_ARG}, {MAX_SCALE_ARG}]."
        )
    return scale


def _format_scale_arg(scale: float) -> str:
    """Format :attr:`Style.scale` back into the canonical tag argument."""
    return f"{scale:g}"


def _parse_shift_arg(arg: str) -> Optional[float]:
    """Parse the optional ``<sup:N>``/``<sub:N>`` shift argument.

    Args:
        arg: The raw ``name:args`` argument text; ``""`` for the bare tag.

    Returns:
        The baseline shift in cap-height units, or ``None`` for the bare
        ``<sup>``/``<sub>`` form (the pipeline derives the shift from the
        cascading ``supersubscript_downscale``).

    Raises:
        MarkupError: If the argument is not a float in
            ``[MIN_SHIFT_ARG, MAX_SHIFT_ARG]`` (fail-loud, like an
            unclosed tag).
    """
    if not arg:
        return None
    try:
        shift = float(arg)
    except ValueError:
        raise MarkupError(
            f"sup/sub tag argument {arg!r} is not a number (expected e.g. <sup:0.3>)."
        ) from None
    if not MIN_SHIFT_ARG <= shift <= MAX_SHIFT_ARG:
        raise MarkupError(
            f"sup/sub tag argument {arg!r} is outside the supported range "
            f"[{MIN_SHIFT_ARG}, {MAX_SHIFT_ARG}]."
        )
    return shift


def _format_sup_sub_arg(sup_sub: Optional[SupSub], direction: int) -> str:
    """Format :attr:`Style.sup_sub` back into the canonical tag argument.

    Returns the empty string for the bare (no-argument) form --
    :func:`serialize_runs` then emits the bare ``<name>``, which re-parses
    to the identical style. The direction guard keeps each tag serializing
    only its own runs (the pair is mutually exclusive by ``is_active``).
    """
    if sup_sub is None or sup_sub.direction != direction:
        return ""
    return "" if sup_sub.shift is None else f"{sup_sub.shift:g}"


def _parse_font_arg(arg: str) -> str:
    """Parse and validate a ``<f:font name>`` argument.

    The tag-argument regex is ``\\S*`` (no whitespace), so ``+`` is the
    canonical space for a font name that contains one (``<f:Foo+Bar>``);
    ``_`` stays literal (catalog file names keep underscores). The name is
    NOT validated here -- markup.py imports no pipeline module -- the
    pipeline's ``font_resolver`` (a :func:`parse_markup` parameter)
    canonicalizes and validates it, raising :class:`MarkupError` for an
    unknown name (fail-loud, like an unclosed tag).

    Args:
        arg: The raw ``name:args`` argument text.

    Returns:
        The decoded font name (``+`` -> space, stripped), unvalidated.

    Raises:
        MarkupError: If the argument is empty (``<f:>``).
    """
    name = arg.replace("+", " ").strip()
    if not name:
        raise MarkupError("font tag argument is empty (expected e.g. <f:Dino>).")
    return name


def _format_font_arg(font: Optional[str]) -> str:
    """Format :attr:`Style.font` back into the canonical tag argument.

    Spaces re-encode as ``+`` (the canonical space of the ``\\S*`` argument
    regex), so the line-content reflow round-trip
    (serialize -> raw text -> re-parse) is exact.
    """
    return "" if font is None else font.replace(" ", "+")


def _parse_cutter_arg(arg: str) -> float:
    """Parse and validate a ``<c:D>`` argument (the cutter diameter, inches).

    The value is in inches, like every other cutter value in the repo
    (``cutter_size``, the ``tools.json`` ``available_cutters`` inventory).
    The syntactic bounds are deliberately loose (a positive diameter up to
    :data:`MAX_CUTTER_ARG`); the real limit is the line's own text height,
    which the pipeline checks at resolution time (a ``CutterSizeError``,
    the same contract an out-of-range line-level ``cutter_size`` obeys).

    Args:
        arg: The raw ``name:args`` argument text.

    Returns:
        The cutter diameter in inches.

    Raises:
        MarkupError: If the argument is not a float in
            ``(MIN_CUTTER_ARG, MAX_CUTTER_ARG]`` (fail-loud, like an
            unclosed tag).
    """
    try:
        cutter = float(arg)
    except ValueError:
        raise MarkupError(
            f"cutter tag argument {arg!r} is not a number (expected e.g. <c:0.06>)."
        ) from None
    if not MIN_CUTTER_ARG < cutter <= MAX_CUTTER_ARG:
        raise MarkupError(
            f"cutter tag argument {arg!r} is outside the supported range "
            f"({MIN_CUTTER_ARG}, {MAX_CUTTER_ARG}]."
        )
    return cutter


def _format_cutter_arg(cutter: Optional[float]) -> str:
    """Format :attr:`Style.cutter` back into the canonical tag argument."""
    return "" if cutter is None else f"{cutter:g}"


def _parse_cutter_buffer_arg(arg: str) -> float:
    """Parse and validate a ``<cb:D>`` argument (the kerf buffer, inches).

    The value is a non-negative width in inches. ``0.0`` is legal: it
    explicitly overrides the line's cascaded buffer to zero for the span.
    The syntactic bounds are deliberately loose (up to :data:`MAX_BUFFER_ARG`
    to catch clear typos); the real limit is that ``cutter + buffer`` must
    leave material to engrave, which the pipeline validates at resolution
    time.

    Args:
        arg: The raw ``name:args`` argument text.

    Returns:
        The kerf buffer in inches.

    Raises:
        MarkupError: If the argument is not a float in
            ``[MIN_BUFFER_ARG, MAX_BUFFER_ARG]`` (fail-loud, like an
            unclosed tag).
    """
    try:
        buffer = float(arg)
    except ValueError:
        raise MarkupError(
            f"cutter buffer tag argument {arg!r} is not a number (expected e.g. <cb:0.015>)."
        ) from None
    if not MIN_BUFFER_ARG <= buffer <= MAX_BUFFER_ARG:
        raise MarkupError(
            f"cutter buffer tag argument {arg!r} is outside the supported range"
            f" [{MIN_BUFFER_ARG}, {MAX_BUFFER_ARG}]."
        )
    return buffer


def _format_cutter_buffer_arg(cutter_buffer: Optional[float]) -> str:
    """Format :attr:`Style.cutter_buffer` back into the canonical tag argument."""
    return "" if cutter_buffer is None else f"{cutter_buffer:g}"


def _parse_text_color_arg(arg: str) -> str:
    """Parse and validate a ``<tc:color>`` argument.

    The argument is the stroke-color name -- the full name (``red``) or the
    1-letter abbreviation (``r``) the ``text_color`` field accepts. The
    name is NOT validated here -- markup.py imports no pipeline module --
    the pipeline's ``color_resolver`` (a :func:`parse_markup` parameter)
    canonicalizes and validates it, raising :class:`MarkupError` for an
    unknown name or the implicit ``none`` (fail-loud, like an unclosed tag).

    Args:
        arg: The raw ``name:args`` argument text.

    Returns:
        The stripped color name, unvalidated.

    Raises:
        MarkupError: If the argument is empty (``<tc:>``).
    """
    name = arg.strip()
    if not name:
        raise MarkupError("text color tag argument is empty (expected e.g. <tc:red>).")
    return name


def _format_text_color_arg(color: Optional[str]) -> str:
    """Format :attr:`Style.text_color` back into the canonical tag argument.

    The resolver stores the canonical full name, so serialization never
    re-abbreviates and the line-content reflow round-trip
    (serialize -> raw text -> re-parse) is exact (canonical names resolve
    to identity, the ``<f:...>`` precedent).
    """
    return "" if color is None else color


@dataclass(frozen=True)
class TagSpec:
    """A registered inline markup tag.

    Attributes:
        name: Tag name as written inside the delimiters (``i`` for ``<i>``).
        apply: Maps ``(enclosing style, argument)`` to the style active
            inside the tag (``arg`` is ``""`` for tags that take none).
            Must be idempotent (applying an already-active tag with the
            same argument is a no-op) so :func:`serialize_runs` can recover
            the tag stack from styles, and so :func:`parse_markup` can
            rebuild the active style by replaying the open tags when a
            crossing close removes a non-innermost opener. An
            :attr:`accepts_args` tag validates its argument here, raising
            :class:`MarkupError` for a bad value.
        is_active: True when the tag is active in the given style. Defaults
            to the flag-tag rule ``apply(style, "") == style`` (applying an
            active tag is a no-op); value tags (``<scale:N>``) supply an
            explicit predicate since their ``apply`` needs the argument.
        accepts_args: True when the ``name:args`` open form (e.g.
            ``<scale:0.5>``) is legal for this tag. Without
            :attr:`args_optional`, a bare ``<name>`` on an args tag is
            literal text (the value is mandatory).
        args_optional: True when the argument is OPTIONAL for an
            :attr:`accepts_args` tag: a bare ``<name>`` IS markup (the
            ``apply`` receives ``""`` and derives its value -- ``<sup>``
            derives its shift from ``supersubscript_downscale``) and the
            canonical :attr:`arg` returns ``""`` for the bare form so
            :func:`serialize_runs` re-emits the bare tag. Ignored when
            :attr:`accepts_args` is False.
        arg: Serializes a style back to the tag's canonical argument (used
            by :func:`serialize_runs` to re-emit ``name:arg``). Required
            when :attr:`accepts_args` is True.
    """

    name: str
    apply: Callable[[Style, str], Style]
    is_active: Callable[[Style], bool]
    accepts_args: bool = False
    args_optional: bool = False
    arg: Optional[Callable[[Style], str]] = None


#: Registered markup tags, keyed by tag name (insertion order = the order
#: :func:`serialize_runs` opens them, keeping round-trips stable).
TAG_REGISTRY: Dict[str, TagSpec] = {}

_TAG_NAME_RE: Pattern[str] = re.compile(r"[A-Za-z]+")
_OPEN_RE: Pattern[str] = re.compile(r"([A-Za-z]+)(?::(\S*))?")
_CLOSE_RE: Pattern[str] = re.compile(r"/([A-Za-z]+)")

#: The font-selection tag name (``<f:font name>``); :func:`parse_markup`
#: canonicalizes its argument through the injected ``font_resolver``.
_FONT_TAG = "f"

#: The stroke-color-selection tag name (``<tc:color>``); :func:`parse_markup`
#: canonicalizes its argument through the injected ``color_resolver``.
_TEXT_COLOR_TAG = "tc"


def register_tag(
    name: str,
    apply: Callable[[Style, str], Style],
    accepts_args: bool = False,
    arg: Optional[Callable[[Style], str]] = None,
    is_active: Optional[Callable[[Style], bool]] = None,
    args_optional: bool = False,
) -> None:
    """Register an inline markup tag (idempotent overwrite by name).

    Args:
        name: Tag name inside the delimiters; ASCII letters only.
        apply: ``(style, arg) -> style`` mapping active inside the tag. Must
            be idempotent (see :attr:`TagSpec.apply`).
        accepts_args: Whether the ``name:args`` open form is legal.
        arg: Style -> canonical argument serializer (required with
            ``accepts_args``).
        is_active: Style -> bool activity predicate. Defaults to the flag-tag
            rule ``apply(style, "") == style``.
        args_optional: Whether the argument is optional on an args tag (the
            bare ``<name>`` is markup, ``apply`` receives ``""``). Ignored
            when ``accepts_args`` is False.

    Raises:
        ValueError: If ``name`` is not one or more ASCII letters, or an
            args tag is registered without an ``arg`` serializer.
    """
    if not _TAG_NAME_RE.fullmatch(name):
        raise ValueError(f"tag name {name!r} must be ASCII letters only")
    if accepts_args and arg is None:
        raise ValueError(f"tag {name!r} accepts args and needs an arg serializer")
    if is_active is None:

        def _flag_active(style: Style) -> bool:
            return apply(style, "") == style

        is_active = _flag_active
    TAG_REGISTRY[name] = TagSpec(
        name=name,
        apply=apply,
        is_active=is_active,
        accepts_args=accepts_args,
        args_optional=args_optional,
        arg=arg,
    )


register_tag("i", lambda style, arg: replace(style, italic=True))
register_tag("u", lambda style, arg: replace(style, underline=True))
register_tag("b", lambda style, arg: replace(style, bold=True))
# Registered LAST so the i/u/b serialization order stays bit-identical.
register_tag(
    "scale",
    lambda style, arg: replace(style, scale=_parse_scale_arg(arg)),
    accepts_args=True,
    arg=lambda style: _format_scale_arg(style.scale),
    is_active=lambda style: style.scale != UNSET_SCALE,
)
# The first optional-argument tags (registered after "scale" so every
# existing serialization order stays bit-identical): a bare <sup>/<sub>
# derives its baseline shift -- and a uniform downscale -- from the
# cascading supersubscript_downscale (resolved by the pipeline, never the
# parser), while <sup:0.3> sets the shift explicitly and applies no scaling.
# The per-direction is_active makes the pair mutually exclusive, so nesting
# <sup>a<sub>b</sub>c</sup> restores <sup> for c for free (the close pops
# the pre-open Style).
register_tag(
    "sup",
    lambda style, arg: replace(style, sup_sub=SupSub(SUP_DIRECTION, _parse_shift_arg(arg))),
    accepts_args=True,
    args_optional=True,
    arg=lambda style: _format_sup_sub_arg(style.sup_sub, SUP_DIRECTION),
    is_active=lambda style: style.sup_sub is not None and style.sup_sub.direction == SUP_DIRECTION,
)
register_tag(
    "sub",
    lambda style, arg: replace(style, sup_sub=SupSub(SUB_DIRECTION, _parse_shift_arg(arg))),
    accepts_args=True,
    args_optional=True,
    arg=lambda style: _format_sup_sub_arg(style.sup_sub, SUB_DIRECTION),
    is_active=lambda style: style.sup_sub is not None and style.sup_sub.direction == SUB_DIRECTION,
)
# Registered LAST so every existing serialization order stays bit-identical.
# A plain flag tag: applying it to an already-struck run is the identity, so
# nesting <s> inside <s> has no additional effect (and the close pops the
# pre-open style, restoring the outer run for the trailing text).
register_tag("s", lambda style, arg: replace(style, strikethrough=True))
# Registered LAST so every existing serialization order stays bit-identical.
# A plain flag tag (no args -- the factor rides the cascading sc_scale field,
# unlike <scale:N>): applying it to an already-small-caps run is the identity,
# so nesting <sc> inside <sc> has no additional effect. The pipeline also
# renders the tag inert on a <base> SC font (the whole line is already small
# caps) and on a symbol font, which has no lowercase to shrink.
register_tag("sc", lambda style, arg: replace(style, small_caps=True))
# Registered LAST so every existing serialization order stays bit-identical.
# The font-selection tag: a value tag (like <scale:N>) whose argument names
# the run's font. A bare <f> is literal text (the name is mandatory, the
# <scale> rule). apply is set-to-value (idempotent), so a nested <f:B> inside
# <f:A> restores A for the trailing text for free (the close pops the
# pre-open Style), and serialize_runs re-emits name:arg via _format_font_arg
# (the reflow round-trip depends on it). The argument is decoded here
# (+ -> space); canonicalization/validation runs through the font_resolver
# injected into parse_markup -- markup.py imports no pipeline module, so an
# unknown font name raises MarkupError at resolution time (CLI non-zero).
register_tag(
    "f",
    lambda style, arg: replace(style, font=_parse_font_arg(arg)),
    accepts_args=True,
    arg=lambda style: _format_font_arg(style.font),
    is_active=lambda style: style.font is not None,
)
# Registered LAST so every existing serialization order stays bit-identical.
# The cutter-selection tag: a value tag (like <scale:N>) whose argument is
# the cutter diameter in inches. A bare <c> is literal text (the diameter is
# mandatory, the <scale> rule). apply is set-to-value (idempotent), so a
# nested <c:0.06> inside <c:0.09> restores 0.09 for the trailing text for
# free (the close pops the pre-open Style), and serialize_runs re-emits
# name:arg via _format_cutter_arg (the reflow round-trip depends on it).
# The argument is validated here (a bad value raises MarkupError -> the CLI
# aborts); the pipeline snaps the value to the shop inventory and checks it
# against the line's text height at resolution time.
register_tag(
    "c",
    lambda style, arg: replace(style, cutter=_parse_cutter_arg(arg)),
    accepts_args=True,
    arg=lambda style: _format_cutter_arg(style.cutter),
    is_active=lambda style: style.cutter is not None,
)
# Registered LAST so every existing serialization order stays bit-identical.
# The original-cutter tag: a plain flag tag (no args -- the pinned tool is
# the line's own, so there is nothing to spell out). The wrapped characters
# engrave with the line's plain cutter and are immune to every
# markup-induced cutter change (the <scale:N> height ladder, the <b> bold
# rung, the bare <sup>/<sub> downscale ladder, the <f:...> run ladder).
# apply is set-to-True (idempotent), so nesting <oc> inside <oc> has no
# additional effect and the close pops the pre-open Style, restoring the
# outer run for the trailing text. The pipeline resolves the pin against
# the line's own cutter (which the compression-driven downsize may move).
register_tag("oc", lambda style, arg: replace(style, original_cutter=True))
# Registered LAST so every existing serialization order stays bit-identical.
# The vertical-centering tag: a plain flag tag (no args -- the target is the
# line's own cap-band midline, so there is nothing to spell out; <vc:0.5> is
# literal text). apply is set-to-True (idempotent), so nesting <vc> inside
# <vc> has no additional effect and the close pops the pre-open Style,
# restoring the outer run for the trailing text. The pipeline measures each
# flagged glyph's rendered ink box (a two-pass render) and rides the
# existing baseline_shifts array with the raise/lower that centres it
# between the baseline and the capline.
register_tag("vc", lambda style, arg: replace(style, v_center=True))
# Registered LAST so every existing serialization order stays bit-identical.
# The stroke-color-selection tag: a value tag (like <f:...>) whose argument
# names the run's text_color layer (full name or 1-letter abbreviation,
# case-insensitive). A bare <tc> is literal text (the color is mandatory,
# the <scale>/<c:> rule). apply is set-to-value (idempotent), so a nested
# <tc:b> inside <tc:r> restores red for the trailing text for free (the
# close pops the pre-open Style), and serialize_runs re-emits name:arg via
# _format_text_color_arg (the reflow round-trip depends on it). The
# argument is decoded here; canonicalization/validation runs through the
# color_resolver injected into parse_markup -- markup.py imports no pipeline
# module, so an unknown color (or the implicit "none") raises MarkupError at
# resolution time (CLI non-zero). The tag is a PURE LAYER tag: it changes no
# geometry, cutter, or height, only the (cutter, color) toolpath layer its
# strokes engrave on (the line-level text_color contract, per character).
register_tag(
    "tc",
    lambda style, arg: replace(style, text_color=_parse_text_color_arg(arg)),
    accepts_args=True,
    arg=lambda style: _format_text_color_arg(style.text_color),
    is_active=lambda style: style.text_color is not None,
)
# Registered LAST so every existing serialization order stays bit-identical.
# The kerf-buffer tag: a value tag (like <c:D>) whose argument is the kerf
# buffer in inches for the run. A bare <cb> is literal text (the buffer is
# mandatory, the <c:>/<scale> rule). apply is set-to-value (idempotent), so
# a nested <cb:0.02> inside <cb:0.01> restores 0.01 for the trailing text
# for free (the close pops the pre-open Style), and serialize_runs re-emits
# name:arg via _format_cutter_buffer_arg (the reflow round-trip depends on
# it). The argument is validated here (a bad value raises MarkupError ->
# the CLI aborts). Unlike <c:D>, this tag carries NO inventory snap and
# adds NO new pen or PLT file -- it is a pure width modifier: the physical
# tool stays the same, only the effective engraved width (cutter + buffer)
# changes. The pipeline uses it to widen per_char_cutters into
# effective_per_char_cutters (and effective_used_cutters) on a per-character
# basis, overriding the line's cascaded cutter_buffer scalar for each span.
register_tag(
    "cb",
    lambda style, arg: replace(style, cutter_buffer=_parse_cutter_buffer_arg(arg)),
    accepts_args=True,
    arg=lambda style: _format_cutter_buffer_arg(style.cutter_buffer),
    is_active=lambda style: style.cutter_buffer is not None,
)


@dataclass(frozen=True)
class Run:
    """A contiguous span of plain text sharing one :class:`Style`.

    Attributes:
        text: The plain (markup-free) text of the run.
        style: The style to render the run with.
    """

    text: str
    style: Style = PLAIN


@dataclass(frozen=True)
class StyledText:
    """The parsed form of one text line's raw ``text``.

    Attributes:
        runs: The line's styled runs; always covers exactly ``plain_text``.
        plain_text: The line's text with all markup removed (what reports,
            chunk records, and layout findings should display).
        has_markup: True when the raw text contained at least one parsed tag
            (callers use this to keep markup-free lines on the historical
            render path, bit-identical).
    """

    runs: Tuple[Run, ...]
    plain_text: str
    has_markup: bool = False

    @property
    def italic_flags(self) -> Tuple[bool, ...]:
        """Per-character italic flags, aligned with :attr:`plain_text`."""
        return tuple(style.italic for style in _per_char_styles(self))

    @property
    def underline_flags(self) -> Tuple[bool, ...]:
        """Per-character underline flags, aligned with :attr:`plain_text`."""
        return tuple(style.underline for style in _per_char_styles(self))

    @property
    def strikethrough_flags(self) -> Tuple[bool, ...]:
        """Per-character strikethrough flags, aligned with :attr:`plain_text`."""
        return tuple(style.strikethrough for style in _per_char_styles(self))

    @property
    def bold_flags(self) -> Tuple[bool, ...]:
        """Per-character bold flags, aligned with :attr:`plain_text`."""
        return tuple(style.bold for style in _per_char_styles(self))

    @property
    def scale_values(self) -> Tuple[float, ...]:
        """Per-character ``<scale:N>`` factors, aligned with :attr:`plain_text`.

        ``1.0`` (:data:`UNSET_SCALE`) marks an unscaled character.
        """
        return tuple(style.scale for style in _per_char_styles(self))

    @property
    def sup_sub_values(self) -> Tuple[Optional[SupSub], ...]:
        """Per-character ``<sup>``/``<sub>`` shifts, aligned with :attr:`plain_text`.

        ``None`` marks an unmarked character. Mirrors :attr:`scale_values`
        so the pipeline derives each character's downscale and baseline
        shift from the style (never a stored positional array).
        """
        return tuple(style.sup_sub for style in _per_char_styles(self))

    @property
    def small_caps_flags(self) -> Tuple[bool, ...]:
        """Per-character ``<sc>`` small-caps flags, aligned with :attr:`plain_text`.

        Mirrors :attr:`underline_flags` / :attr:`strikethrough_flags`: the
        selection is DERIVED from the runs, so a pre-pass that rewrites the
        line's text (``with_markup``, the line-content reflow) keeps every
        run on its own style.
        """
        return tuple(style.small_caps for style in _per_char_styles(self))

    @property
    def font_values(self) -> Tuple[Optional[str], ...]:
        """Per-character ``<f:...>`` font names, aligned with :attr:`plain_text`.

        ``None`` at every position the line's own cascaded ``font`` engraves
        (every character outside an ``<f:...>`` span); the canonical font name
        inside one. The per-character sibling of :attr:`italic_flags`, derived
        from the runs like every other flag array, so a pre-pass that rewrites
        the line's text (``with_markup``, the line-content reflow) keeps every
        run on its own font.
        """
        return tuple(style.font for style in _per_char_styles(self))

    @property
    def cutter_values(self) -> Tuple[Optional[float], ...]:
        """Per-character ``<c:...>`` cutter diameters, aligned with :attr:`plain_text`.

        ``None`` at every position the line's own cascaded cutter engraves
        (every character outside an ``<c:...>`` span); the requested cutter
        diameter in inches inside one. The per-character sibling of
        :attr:`font_values`, derived from the runs like every other flag
        array, so a pre-pass that rewrites the line's text (``with_markup``,
        the line-content reflow) keeps every run on its own tool.
        """
        return tuple(style.cutter for style in _per_char_styles(self))

    @property
    def original_cutter_flags(self) -> Tuple[bool, ...]:
        """Per-character ``<oc>`` flags, aligned with :attr:`plain_text`.

        ``True`` at every position the character is wrapped in an
        ``<oc>...</oc>`` span (and therefore engraves with the line's own
        plain cutter, immune to markup-induced cutter changes). Mirrors
        :attr:`bold_flags`: the selection is DERIVED from the runs, so a
        pre-pass that rewrites the line's text (``with_markup``, the
        line-content reflow) keeps every run pinned.
        """
        return tuple(style.original_cutter for style in _per_char_styles(self))

    @property
    def v_center_flags(self) -> Tuple[bool, ...]:
        """Per-character ``<vc>`` vertical-centering flags, aligned with
        :attr:`plain_text`.

        ``True`` at every position the character is wrapped in a
        ``<vc>...</vc>`` span (and therefore engraves raised or lowered so
        its ink is centred between the baseline and the capline). Mirrors
        :attr:`original_cutter_flags`: the selection is DERIVED from the
        runs, so a pre-pass that rewrites the line's text (``with_markup``,
        the line-content reflow) keeps every run centred.
        """
        return tuple(style.v_center for style in _per_char_styles(self))

    @property
    def text_color_values(self) -> Tuple[Optional[str], ...]:
        """Per-character ``<tc:...>`` stroke-color names, aligned with
        :attr:`plain_text`.

        ``None`` at every position the line's own cascaded ``text_color``
        engraves (every character outside a ``<tc:...>`` span); the canonical
        color name inside one. The per-character sibling of
        :attr:`font_values`, derived from the runs like every other flag
        array, so a pre-pass that rewrites the line's text (``with_markup``,
        the line-content reflow) keeps every run on its own layer.
        """
        return tuple(style.text_color for style in _per_char_styles(self))

    @property
    def cutter_buffer_values(self) -> Tuple[Optional[float], ...]:
        """Per-character ``<cb:...>`` kerf buffer overrides, aligned with
        :attr:`plain_text`.

        ``None`` at every position the line's own cascaded buffer applies
        (every character outside a ``<cb:...>`` span); the override value in
        inches inside one. The per-character sibling of :attr:`cutter_values`,
        derived from the runs like every other flag array, so a pre-pass that
        rewrites the line's text (``with_markup``, the line-content reflow)
        keeps every run on its own buffer.
        """
        return tuple(style.cutter_buffer for style in _per_char_styles(self))

    @property
    def kern_values(self) -> Tuple[int, ...]:
        """Per-character signed manual-kern marker counts, aligned with
        :attr:`plain_text`.

        ``0`` at every position no ``kern_pos_string`` / ``kern_neg_string``
        marker precedes; ``+1`` per positive marker, ``-1`` per negative
        marker (adjacent markers accumulate). The marker strings themselves
        never appear in :attr:`plain_text` -- :func:`parse_markup` strips
        them -- so this array is the exact per-character cursor-delta the
        pipeline multiplies by ``kern_width_fraction * cap height``. Mirrors
        :attr:`scale_values`: the count rides the runs, so a pre-pass that
        rewrites the line's text (``with_markup``, the line-content reflow)
        keeps every marker on its own character.
        """
        return tuple(style.kern for style in _per_char_styles(self))


@dataclass(frozen=True)
class Piece:
    """A whitespace-free styled fragment of one word (reflow token part)."""

    text: str
    style: Style = PLAIN


@dataclass(frozen=True)
class StyledWord:
    """One whitespace-delimited styled word of a parsed line.

    Attributes:
        pieces: The word's styled fragments (one per style change). A
            single piece means the word is style-uniform.
        text: The word's plain text (all pieces concatenated).
    """

    pieces: Tuple[Piece, ...]
    text: str

    @property
    def style(self) -> Style:
        """The style of the word's first piece.

        Words are style-uniform whenever :func:`style_boundaries` shows no
        style change inside a word, which is the guard the reflow enforces.
        """
        return self.pieces[0].style


def _merge_runs(runs: Sequence[Run]) -> Tuple[Run, ...]:
    """Merge adjacent runs sharing a style and drop empty runs.

    Args:
        runs: Runs in document order (may contain empties / neighbours with
            equal styles produced by the scanner).

    Returns:
        A minimal tuple of runs; never contains empty runs, and no two
        adjacent runs share a style.
    """
    merged: List[Run] = []
    for run in runs:
        if not run.text:
            continue
        if merged and merged[-1].style == run.style:
            merged[-1] = Run(merged[-1].text + run.text, run.style)
        else:
            merged.append(run)
    return tuple(merged)


def _per_char_styles(styled: StyledText) -> Tuple[Style, ...]:
    """Expand :attr:`StyledText.runs` into one style per plain-text character."""
    styles: List[Style] = []
    for run in styled.runs:
        styles.extend([run.style] * len(run.text))
    return tuple(styles)


def _strip_kern_markers(
    runs: Sequence[Run],
    kern_pos: Optional[str],
    kern_neg: Optional[str],
) -> Tuple[Tuple[Run, ...], int]:
    """Strip manual-kern marker strings from runs, attaching signed counts.

    Runs the leftmost-first scan over the runs' concatenated plain text:
    every ``kern_pos`` occurrence counts ``+1`` and every ``kern_neg``
    occurrence counts ``-1`` (checked in that order, so a positive marker
    wins a shared prefix). The marker characters are REMOVED from the text
    and the accumulated count attaches to the style of the next
    NON-SPACE character (``Style.kern``), so ``plain_text`` stays
    marker-free and every per-character array keeps its 1:1 alignment.
    A marker with no following non-space character (end of line, or only
    whitespace to the end) is dropped -- there is nothing to kern.

    Args:
        runs: The merged runs from the tag scan (document order).
        kern_pos: The positive marker string, or ``None`` (off).
        kern_neg: The negative marker string, or ``None`` (off).

    Returns:
        ``(stripped_runs, removed_characters)`` -- the runs rebuilt after
        the removal (adjacent equal styles merged), and the number of
        characters removed (0 means the line carried no markers, so callers
        keep the historical bit-identical path).
    """
    plain = "".join(run.text for run in runs)
    styles: List[Style] = []
    for run in runs:
        styles.extend([run.style] * len(run.text))
    kept_chars: List[str] = []
    kept_styles: List[Style] = []
    pending = 0
    removed = 0
    index = 0
    length = len(plain)
    while index < length:
        if kern_pos and plain.startswith(kern_pos, index):
            pending += 1
            removed += len(kern_pos)
            index += len(kern_pos)
            continue
        if kern_neg and plain.startswith(kern_neg, index):
            pending -= 1
            removed += len(kern_neg)
            index += len(kern_neg)
            continue
        style = styles[index]
        if pending and not plain[index].isspace():
            style = replace(style, kern=style.kern + pending)
            pending = 0
        kept_chars.append(plain[index])
        kept_styles.append(style)
        index += 1
    # A trailing marker (pending != 0) has no following non-space character:
    # dropped, exactly like a marker at the very end of the line.
    merged: List[Run] = []
    for char, style in zip(kept_chars, kept_styles):
        if merged and merged[-1].style == style:
            merged[-1] = Run(merged[-1].text + char, style)
        else:
            merged.append(Run(char, style))
    return tuple(merged), removed


def split_brackets(html_brackets: str) -> Tuple[str, str]:
    """Split an ``html_brackets`` value into its left/right delimiters.

    The value is an even-length string whose first half is the left
    delimiter and second half the right delimiter (the schema validator is
    the authority on well-formedness; this helper only splits).

    Args:
        html_brackets: The configured bracket pair (even length, halves
            differing).

    Returns:
        The ``(left, right)`` delimiter pair.

    Raises:
        ValueError: If the length is odd (direct API callers bypassing the
            schema validator).
    """
    if len(html_brackets) % 2 != 0:
        raise ValueError(
            f"html_brackets must have an even number of characters; got {html_brackets!r}"
        )
    half = len(html_brackets) // 2
    return html_brackets[:half], html_brackets[half:]


def parse_markup(
    text: str,
    left: str = "<",
    right: str = ">",
    font_resolver: Optional[Callable[[str], str]] = None,
    kern_pos: Optional[str] = None,
    kern_neg: Optional[str] = None,
    color_resolver: Optional[Callable[[str], str]] = None,
) -> StyledText:
    """Parse one text line's raw text into styled runs.

    Delimiters are literal strings (the two halves of ``html_brackets``).
    Scanning is leftmost-first: at each left delimiter, the body up to the
    first right delimiter is a tag only when it matches a registered tag
    name (open) or ``/name`` (close); any other body -- and any left
    delimiter with no right delimiter after it -- is literal text.

    After the tag scan, the manual-kern marker strings (``kern_pos`` /
    ``kern_neg``, the cascading ``kern_pos_string`` / ``kern_neg_string``
    fields) are stripped from the literal text by
    :func:`_strip_kern_markers`: every occurrence is REMOVED and a signed
    count attaches to the following character's :attr:`Style.kern` (the
    pipeline turns the count into a cursor shift). Because stripping runs
    AFTER the scan, a marker only matches literal text -- ``<->`` is a legal
    marker (unregistered body), while a marker that is itself a registered
    tag (``<s>``) is consumed as markup and never strips (the schema
    validator rejects such a marker at spec level).

    Args:
        text: The raw line text (may contain markup).
        left: Left delimiter (first half of ``html_brackets``).
        right: Right delimiter (second half of ``html_brackets``).
        font_resolver: Optional ``(decoded name -> canonical name)`` mapping
            applied to every ``<f:...>`` argument at open time (the stack
            stores the canonical name, so the crossing-close replay reuses it
            for free). The pipeline injects the font-catalog lookup; markup.py
            imports no pipeline module, so an unknown font name raises
            :class:`MarkupError` here (fail-loud, like an unclosed tag). A
            resolver's own :class:`ValueError` is wrapped. ``None`` (direct
            API / test callers) keeps the decoded argument verbatim.
        kern_pos: The positive manual-kern marker string
            (``kern_pos_string``), or ``None`` (disabled).
        kern_neg: The negative manual-kern marker string
            (``kern_neg_string``), or ``None`` (disabled).
        color_resolver: Optional ``(raw color name -> canonical color name)``
            mapping applied to every ``<tc:...>`` argument at open time (the
            stack stores the canonical name, so the crossing-close replay
            reuses it for free). The pipeline injects the ``TextColor``
            lookup (full name or 1-letter abbreviation, case-insensitive);
            markup.py imports no pipeline module, so an unknown color -- or
            the implicit ``none`` -- raises :class:`MarkupError` here
            (fail-loud, like an unclosed tag). A resolver's own
            :class:`ValueError` is wrapped. ``None`` (direct API / test
            callers) keeps the raw argument verbatim.

    Returns:
        The parsed :class:`StyledText`. A text with no parsed tag returns a
        single plain run with ``has_markup=False``. Stripping at least one
        kern marker sets ``has_markup=True`` (the line's runs carry
        :attr:`Style.kern` counts, so it must route through the exact
        per-character render path, the ``enforce_cap_line`` gate
        precedent).

    Crossing spans are legal: a close tag closes the most recent open tag
    with that name (not necessarily the innermost), and each tag applies to
    its own open..close range, so the style of a character is the
    intersection of the spans covering it (HTML-style).

    Raises:
        MarkupError: If a close tag's name was never opened, a registered
            close tag appears with no open tag at all, or an open tag is
            never closed.
        ValueError: If ``left``/``right`` are empty or identical (schema
            validation is the real gate; this guards direct API callers).
    """
    if not left or not right:
        raise ValueError("markup delimiters must be non-empty strings")
    if left == right:
        raise ValueError("markup left and right delimiters must differ")

    runs: List[Run] = []
    # Open tags (innermost last): (name, argument, open pos). The close
    # branch replays the remaining opens to rebuild the active style, so
    # the argument rides the stack.
    stack: List[Tuple[str, str, int]] = []
    current = PLAIN
    cursor = 0
    length = len(text)
    has_markup = False

    def flush(upto: int, style: Style) -> None:
        if upto > cursor:
            runs.append(Run(text[cursor:upto], style))

    while cursor < length:
        open_at = text.find(left, cursor)
        if open_at < 0:
            break
        close_at = text.find(right, open_at + len(left))
        if close_at < 0:
            break  # No closing delimiter: the remainder is literal text.
        body = text[open_at + len(left) : close_at]

        close_match = _CLOSE_RE.fullmatch(body)
        open_match = _OPEN_RE.fullmatch(body)
        is_close = close_match is not None
        spec: Optional[TagSpec] = None
        if not is_close and open_match is not None:
            spec = TAG_REGISTRY.get(open_match.group(1))
            if spec is not None and open_match.group(2) is not None:
                if not spec.accepts_args:
                    # ``name:args`` on a tag that takes no args is not markup.
                    spec = None
            elif spec is not None and spec.accepts_args and not spec.args_optional:
                # A bare ``<name>`` on a mandatory-arg tag is not markup --
                # the value is mandatory (``<scale>`` engraves literally).
                # An ``args_optional`` tag (``<sup>``) keeps the bare form:
                # ``apply`` receives ``""`` and derives its value.
                spec = None

        if spec is None and not is_close:
            # Not markup at all (unregistered name, non-tag body): the left
            # delimiter is literal; keep scanning after it (leftmost wins).
            flush(open_at, current)
            runs.append(Run(left, current))
            cursor = open_at + len(left)
            continue

        flush(open_at, current)
        if is_close:
            assert close_match is not None
            name = close_match.group(1)
            if name not in TAG_REGISTRY:
                # Close tag for an unregistered name: literal text (its open
                # counterpart was literal too, by the rule above).
                runs.append(Run(text[open_at : close_at + len(right)], current))
                cursor = close_at + len(right)
                continue
            if not stack:
                raise MarkupError(
                    f"Unexpected closing tag {left}/{name}{right} at position "
                    f"{open_at} in text {text!r} (no matching open tag)."
                )
            # The most recent open tag with this name (crossing spans are
            # legal: the close need not meet the innermost opener). The
            # earliest match scanning from the top is the LAST open with
            # the name, so a duplicate open (``<i>a<i>b</i>c</i>``) closes
            # its innermost instance.
            open_index = -1
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == name:
                    open_index = index
                    break
            if open_index < 0:
                raise MarkupError(
                    f"Closing tag {left}/{name}{right} at position {open_at} "
                    f"in text {text!r} never opened (no open {left}{name}{right} "
                    f"to close)."
                )
            stack.pop(open_index)
            # Rebuild the active style by replaying the remaining opens onto
            # PLAIN. Invariant: ``current`` is always the replay of the stack
            # (each open composes onto the replay of its prefix), so the
            # replay reproduces the pre-close style minus the closed tag
            # exactly -- for the innermost close AND for crossing closes.
            current = PLAIN
            for open_name, open_arg, _ in stack:
                current = TAG_REGISTRY[open_name].apply(current, open_arg)
        else:
            assert spec is not None
            assert open_match is not None
            open_arg = open_match.group(2) or ""
            if spec.name == _FONT_TAG and font_resolver is not None:
                # Canonicalize at open time; the stack carries the canonical
                # name, so every replay (crossing closes) reproduces it
                # without re-resolving. Decoding (+ -> space) happens in
                # apply; canonical font names contain no '+', so decoding a
                # canonical name is the identity (idempotency preserved).
                try:
                    open_arg = font_resolver(_parse_font_arg(open_arg))
                except (MarkupError, ValueError) as exc:
                    raise MarkupError(
                        f"Invalid tag {left}{body}{right} at position "
                        f"{open_at} in text {text!r}: {exc}"
                    ) from None
            if spec.name == _TEXT_COLOR_TAG and color_resolver is not None:
                # The <tc:color> mirror of the font resolver: canonicalize
                # (and validate) at open time so the stack carries the
                # canonical full name, every replay reuses it, and
                # serialize_runs re-emits the canonical form (the reflow
                # round-trip resolves a canonical name to identity).
                try:
                    open_arg = color_resolver(_parse_text_color_arg(open_arg))
                except (MarkupError, ValueError) as exc:
                    raise MarkupError(
                        f"Invalid tag {left}{body}{right} at position "
                        f"{open_at} in text {text!r}: {exc}"
                    ) from None
            stack.append((spec.name, open_arg, open_at))
            try:
                current = spec.apply(current, open_arg)
            except MarkupError as exc:
                raise MarkupError(
                    f"Invalid tag {left}{body}{right} at position {open_at} in text {text!r}: {exc}"
                ) from None
        has_markup = True
        cursor = close_at + len(right)

    flush(length, current)

    if stack:
        name, _, open_pos = stack[-1]
        raise MarkupError(
            f"Unclosed tag {left}{name}{right} at position {open_pos} in text {text!r}."
        )

    merged = _merge_runs(runs)
    if kern_pos or kern_neg:
        merged, removed = _strip_kern_markers(merged, kern_pos, kern_neg)
        if removed:
            has_markup = True
    plain = "".join(run.text for run in merged)
    return StyledText(runs=merged, plain_text=plain, has_markup=has_markup)


def serialize_runs(
    runs: Sequence[Run],
    left: str = "<",
    right: str = ">",
    kern_pos: Optional[str] = None,
    kern_neg: Optional[str] = None,
) -> str:
    """Rebuild raw markup text from styled runs (minimal tag nesting).

    Walks the runs with a tag stack: at each style change, registered tags
    whose :attr:`TagSpec.is_active` predicate holds in the target style are
    opened (registry order) and tags no longer active are closed (LIFO,
    newest first). Flag tags emit ``<name>``; an :attr:`accepts_args` tag
    re-emits its canonical value through :attr:`TagSpec.arg`
    (``<scale:0.5>``), so the line-content reflow round-trips the argument
    through the raw text. The nesting order the registry defines re-parses
    to the same runs.

    A run whose style carries a nonzero :attr:`Style.kern` count re-emits
    ``abs(kern)`` copies of the marker string before the run's text (the
    positive string for ``kern > 0``, the negative for ``kern < 0``), so a
    manual-kern marker survives the reflow's raw-text round-trip (the
    ``<scale:N>`` argument-serializer precedent). The markers emit INSIDE
    the run's open tags, immediately before its text: re-parsing strips
    them from the literal span and re-attaches the count to the following
    character, reproducing the runs exactly. ``kern_pos``/``kern_neg``
    ``None`` (the direct-API default) drops a kern count with no marker to
    emit -- callers round-tripping kerned text must pass the strings.

    Args:
        runs: The runs to serialize (document order).
        left: Left delimiter to emit.
        right: Right delimiter to emit.
        kern_pos: The positive manual-kern marker string to re-emit.
        kern_neg: The negative manual-kern marker string to re-emit.

    Returns:
        The raw markup text (no tags when every run is :data:`PLAIN`).
    """
    out: List[str] = []
    open_tags: List[str] = []  # LIFO stack of open tag names
    open_styles: List[Style] = []  # style active inside each open tag

    for run in runs:
        if not run.text:
            continue
        # Close tags (LIFO) whose style is not the target style.
        while open_tags and open_styles[-1] != run.style:
            top = open_tags.pop()
            open_styles.pop()
            out.append(f"{left}/{top}{right}")
        # The style active on the stack after the closes above; the opens
        # below compose onto it, so each tag's "already active?" test reads
        # the stack as it stands -- not the style a sibling open just
        # produced (which would hide a tag nested inside an earlier one).
        current = open_styles[-1] if open_styles else PLAIN
        # Open registered tags missing for the target style (registry order).
        for spec in TAG_REGISTRY.values():
            if not spec.is_active(run.style):
                continue  # Tag not active in the target style.
            if spec.is_active(current):
                continue  # Already active on the current stack.
            arg_text = spec.arg(run.style) if spec.arg is not None else ""
            # An empty canonical arg is the bare form (``<sup>`` derives its
            # value), so no ``:arg`` is emitted -- ``scale``'s canonical arg
            # is never empty while active.
            name = f"{spec.name}:{arg_text}" if spec.accepts_args and arg_text else spec.name
            out.append(f"{left}{name}{right}")
            open_tags.append(spec.name)
            open_styles.append(run.style)
            current = spec.apply(current, arg_text)
        if run.style.kern:
            marker = kern_pos if run.style.kern > 0 else kern_neg
            if marker:
                out.append(marker * abs(run.style.kern))
        out.append(run.text)
    while open_tags:
        top = open_tags.pop()
        out.append(f"{left}/{top}{right}")
    return "".join(out)


def serialize_word(
    word: StyledWord,
    left: str = "<",
    right: str = ">",
    kern_pos: Optional[str] = None,
    kern_neg: Optional[str] = None,
) -> str:
    """Serialize one styled word back to raw markup text.

    A style-uniform word becomes ``<i>word</i>`` (italic) or the bare word
    (plain); a multi-piece word serializes each piece with its own tags, so
    the result re-parses to the same pieces. The line-content reflow uses
    this to move styled words between lines as atomic tokens.

    Args:
        word: The styled word to serialize.
        left: Left delimiter to emit.
        right: Right delimiter to emit.
        kern_pos: The positive manual-kern marker string to re-emit.
        kern_neg: The negative manual-kern marker string to re-emit.

    Returns:
        The raw markup text of the word.
    """
    runs = [Run(piece.text, piece.style) for piece in word.pieces]
    return serialize_runs(runs, left, right, kern_pos, kern_neg)


def split_styled_words(styled: StyledText) -> Tuple[StyledWord, ...]:
    """Split a parsed line into whitespace-delimited styled words.

    Whitespace is dropped (the reflow joins words on a single space,
    matching the plain-text ``" ".join(words)`` rebuild). A word carries one
    :class:`Piece` per style change; :func:`style_boundaries` tells callers
    whether any tag span crossed a word boundary.

    Args:
        styled: The parsed line.

    Returns:
        The words in document order (empty when the line is blank).
    """
    styles = _per_char_styles(styled)
    plain = styled.plain_text
    words: List[StyledWord] = []
    index = 0
    length = len(plain)
    while index < length:
        while index < length and plain[index].isspace():
            index += 1
        if index >= length:
            break
        start = index
        pieces: List[Piece] = []
        while index < length and not plain[index].isspace():
            style = styles[index]
            piece_start = index
            while index < length and not plain[index].isspace() and styles[index] == style:
                index += 1
            pieces.append(Piece(plain[piece_start:index], style))
        words.append(StyledWord(pieces=tuple(pieces), text=plain[start:index]))
    return tuple(words)


def split_styled_words_with_separators(
    styled: StyledText,
) -> Tuple[Tuple[StyledWord, ...], Tuple[Style, ...]]:
    """Split a parsed line into styled words plus the styles of their separators.

    The line-content reflow joins words with a single space; a tag span that
    covers a space (``<u>foo bar</u>``) styles that space, and a plain
    ``" ".join`` of per-word serialized tokens would drop it (the joined
    space becomes unstyled, so a ``<u>`` run splits in two). This variant
    returns the separator styles alongside the words so the reflow can rebuild
    the text with a :func:`serialize_runs` pass that keeps the span continuous.

    Separators are runs of whitespace between words, each collapsed to a
    single space (matching the reflow's join): the returned tuple has exactly
    ``len(words) - 1`` entries, the style of the whitespace between
    consecutive words. Leading/trailing whitespace is dropped. Whitespace
    inside a tag span carries that span's style; whitespace between two
    differently styled words carries the PLAIN style.

    Args:
        styled: The parsed line.

    Returns:
        ``(words, separator_styles)`` in document order.
    """
    styles = _per_char_styles(styled)
    plain = styled.plain_text
    words: List[StyledWord] = []
    separators: List[Style] = []
    index = 0
    length = len(plain)
    while index < length:
        while index < length and plain[index].isspace():
            index += 1
        if index >= length:
            break
        if words:
            # The whitespace run just skipped is this word's separator; its
            # style is the style of its first character (a run of whitespace
            # inside one tag span is style-uniform, and a boundary between
            # differently styled words is PLAIN on the space itself).
            separators.append(styles[index - 1])
        start = index
        pieces: List[Piece] = []
        while index < length and not plain[index].isspace():
            style = styles[index]
            piece_start = index
            while index < length and not plain[index].isspace() and styles[index] == style:
                index += 1
            pieces.append(Piece(plain[piece_start:index], style))
        words.append(StyledWord(pieces=tuple(pieces), text=plain[start:index]))
    return tuple(words), tuple(separators)


def style_boundaries(styled: StyledText) -> Tuple[int, ...]:
    """Plain-text indices at which the active style changes.

    The line-content reflow uses this as a scope guard: a style change at
    index ``i`` means a tag span starts (``style[i]`` italic, ``style[i-1]``
    plain) or ends (the reverse) *between* characters ``i-1`` and ``i``. A
    boundary inside a word (no whitespace at ``i-1``/``i``) means a span
    crosses a word boundary, which makes that group's reflow a no-op,
    because words move between lines as atomic tokens.

    Args:
        styled: The parsed line.

    Returns:
        Ascending indices ``i`` where ``style[i] != style[i - 1]``
        (empty for a plain line).
    """
    styles = _per_char_styles(styled)
    return tuple(i for i in range(1, len(styles)) if styles[i] != styles[i - 1])
