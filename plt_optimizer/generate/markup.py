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
* **Hard errors on broken markup** -- a close tag that does not match the
  innermost open tag, or an open tag never closed, raises :class:`MarkupError`
  (a ``ValueError`` subclass) so the job aborts loudly, consistent with the
  repo's fail-loud philosophy (unknown font, missing glyph).

Tag registry
------------
Markup types are registered in :data:`TAG_REGISTRY` as :class:`TagSpec`
entries (name + a ``style -> style`` mapping + an ``accepts_args`` flag for
the reserved ``name:args`` syntax, e.g. the planned ``<sup:0.3>``). Adding a
markup type (``<b>``, ``<u>``, ``<sc>``, ...) is a one-line registry entry
plus the render-side handling of the new :class:`Style` field; the parser,
cascade, and serialization need no change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Pattern, Sequence, Tuple

__all__ = [
    "MarkupError",
    "PLAIN",
    "Piece",
    "Run",
    "StyledText",
    "StyledWord",
    "Style",
    "TAG_REGISTRY",
    "TagSpec",
    "parse_markup",
    "register_tag",
    "serialize_runs",
    "serialize_word",
    "split_brackets",
    "split_styled_words",
    "style_boundaries",
]


class MarkupError(ValueError):
    """Raised when inline markup is malformed (mismatched/unclosed tag).

    Subclass of :class:`ValueError` so the CLI surfaces the abort as a
    non-zero exit, like the other spec-level validation errors.
    """


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
    """

    italic: bool = False
    underline: bool = False


#: The style of unmarked text (every field at its default).
PLAIN = Style()


@dataclass(frozen=True)
class TagSpec:
    """A registered inline markup tag.

    Attributes:
        name: Tag name as written inside the delimiters (``i`` for ``<i>``).
        apply: Maps the enclosing style to the style active inside the tag.
            Must be idempotent (applying an already-active tag is a no-op)
            so :func:`serialize_runs` can recover the tag stack from styles.
        accepts_args: True when the reserved ``name:args`` open form
            (e.g. ``<sup:0.3>``) is legal for this tag. ``i`` is False;
            argument parsing is not implemented and no shipped tag accepts
            args yet.
    """

    name: str
    apply: Callable[[Style], Style]
    accepts_args: bool = False


#: Registered markup tags, keyed by tag name (insertion order = the order
#: :func:`serialize_runs` opens them, keeping round-trips stable).
TAG_REGISTRY: Dict[str, TagSpec] = {}

_TAG_NAME_RE: Pattern[str] = re.compile(r"[A-Za-z]+")
_OPEN_RE: Pattern[str] = re.compile(r"([A-Za-z]+)(?::(\S*))?")
_CLOSE_RE: Pattern[str] = re.compile(r"/([A-Za-z]+)")


def register_tag(
    name: str,
    apply: Callable[[Style], Style],
    accepts_args: bool = False,
) -> None:
    """Register an inline markup tag (idempotent overwrite by name).

    Args:
        name: Tag name inside the delimiters; ASCII letters only.
        apply: ``style -> style`` mapping active inside the tag. Must be
            idempotent (see :attr:`TagSpec.apply`).
        accepts_args: Whether the reserved ``name:args`` open form is legal.

    Raises:
        ValueError: If ``name`` is not one or more ASCII letters.
    """
    if not _TAG_NAME_RE.fullmatch(name):
        raise ValueError(f"tag name {name!r} must be ASCII letters only")
    TAG_REGISTRY[name] = TagSpec(name=name, apply=apply, accepts_args=accepts_args)


register_tag("i", lambda style: replace(style, italic=True))
register_tag("u", lambda style: replace(style, underline=True))


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

    Raises:
        MarkupError: If a close tag does not match the innermost open tag,
            an open tag is never closed, or a registered close tag appears
            with no open tag at all.
        ValueError: If ``left``/``right`` are empty or identical (schema
            validation is the real gate; this guards direct API callers).
    """
    if not left or not right:
        raise ValueError("markup delimiters must be non-empty strings")
    if left == right:
        raise ValueError("markup left and right delimiters must differ")

    runs: List[Run] = []
    stack: List[Tuple[str, Style, int]] = []  # (name, style-before, open pos)
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
            if spec is not None and not spec.accepts_args and open_match.group(2) is not None:
                # ``name:args`` on a tag that takes no args is not markup.
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
            open_name, popped_style, open_pos = stack.pop()
            if open_name != name:
                raise MarkupError(
                    f"Closing tag {left}/{name}{right} at position {open_at} "
                    f"does not match open tag {left}{open_name}{right} at "
                    f"position {open_pos} in text {text!r}."
                )
            current = popped_style
        else:
            assert spec is not None
            stack.append((spec.name, current, open_at))
            current = spec.apply(current)
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
    whose ``apply`` is *active* in the target style are opened (registry
    order) and tags whose style no longer matches are closed (LIFO, newest
    first). With the shipped single-tag registry this emits ``<i>``/``</i>``
    pairs; future tags round-trip with the nesting order the registry
    defines, so the output re-parses to the same runs.

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
            if spec.apply(run.style) != run.style:
                continue  # Tag not active in the target style.
            if spec.apply(current) == current:
                continue  # Already active on the current stack.
            out.append(f"{left}{spec.name}{right}")
            open_tags.append(spec.name)
            open_styles.append(run.style)
            current = spec.apply(current)
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
