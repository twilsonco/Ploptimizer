"""Inline HTML-like markup parsing for label text lines.

This module is the **framework** for the inline markup syntax described in
``AGENTS.md`` (section 6, "Inline Markup & Italic"). It splits a text line's
raw ``text`` into :class:`Run` s of (plain text, :class:`Style`), so each run
can be rendered with its own typographic transform (the first markup being
``<i>...</i>``, italicization).

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
for the ``name:args`` syntax, e.g. ``<scale:0.5>``). A tag that accepts args
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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Pattern, Sequence, Tuple

__all__ = [
    "MAX_SCALE_ARG",
    "MAX_SHIFT_ARG",
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
            (``non_text_fonts.txt``), which has no lowercase to shrink.
    """

    italic: bool = False
    underline: bool = False
    bold: bool = False
    scale: float = 1.0
    sup_sub: Optional[SupSub] = None
    strikethrough: bool = False
    small_caps: bool = False


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
) -> StyledText:
    """Parse one text line's raw text into styled runs.

    Delimiters are literal strings (the two halves of ``html_brackets``).
    Scanning is leftmost-first: at each left delimiter, the body up to the
    first right delimiter is a tag only when it matches a registered tag
    name (open) or ``/name`` (close); any other body -- and any left
    delimiter with no right delimiter after it -- is literal text.

    Args:
        text: The raw line text (may contain markup).
        left: Left delimiter (first half of ``html_brackets``).
        right: Right delimiter (second half of ``html_brackets``).

    Returns:
        The parsed :class:`StyledText`. A text with no parsed tag returns a
        single plain run with ``has_markup=False``.

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
            stack.append((spec.name, open_arg, open_at))
            try:
                current = spec.apply(current, open_match.group(2) or "")
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
    plain = "".join(run.text for run in merged)
    return StyledText(runs=merged, plain_text=plain, has_markup=has_markup)


def serialize_runs(
    runs: Sequence[Run],
    left: str = "<",
    right: str = ">",
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

    Args:
        runs: The runs to serialize (document order).
        left: Left delimiter to emit.
        right: Right delimiter to emit.

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
        out.append(run.text)
    while open_tags:
        top = open_tags.pop()
        out.append(f"{left}/{top}{right}")
    return "".join(out)


def serialize_word(word: StyledWord, left: str = "<", right: str = ">") -> str:
    """Serialize one styled word back to raw markup text.

    A style-uniform word becomes ``<i>word</i>`` (italic) or the bare word
    (plain); a multi-piece word serializes each piece with its own tags, so
    the result re-parses to the same pieces. The line-content reflow uses
    this to move styled words between lines as atomic tokens.

    Args:
        word: The styled word to serialize.
        left: Left delimiter to emit.
        right: Right delimiter to emit.

    Returns:
        The raw markup text of the word.
    """
    runs = [Run(piece.text, piece.style) for piece in word.pieces]
    return serialize_runs(runs, left, right)


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
