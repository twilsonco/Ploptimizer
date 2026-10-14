"""Tests for the inline markup parser (:mod:`plt_optimizer.generate.markup`).

Covers the framework's contract: literal-first scanning, configurable
multi-character delimiters, hard errors on broken markup, the run/word
partitions the renderers and the reflow consume, and the serialize round-trip.
"""

from __future__ import annotations

import pytest

from plt_optimizer.generate.markup import (
    PLAIN,
    SUB_DIRECTION,
    SUP_DIRECTION,
    TAG_REGISTRY,
    MarkupError,
    Run,
    Style,
    SupSub,
    parse_markup,
    register_tag,
    serialize_runs,
    serialize_word,
    split_brackets,
    split_styled_words,
    split_styled_words_with_separators,
    style_boundaries,
)

ITALIC = Style(italic=True)
UNDERLINE = Style(underline=True)
BOLD = Style(bold=True)


class TestParsePlain:
    """Text without markup keeps the historical single-run shape."""

    def test_plain_text_is_one_plain_run(self) -> None:
        """A tag-free line yields one plain run and has_markup False."""
        styled = parse_markup("HELLO WORLD")
        assert styled.runs == (Run("HELLO WORLD", PLAIN),)
        assert styled.plain_text == "HELLO WORLD"
        assert styled.has_markup is False

    def test_empty_text(self) -> None:
        """Blank input yields no runs and an empty plain text."""
        styled = parse_markup("")
        assert styled.runs == ()
        assert styled.plain_text == ""
        assert styled.has_markup is False

    def test_lone_angle_is_literal(self) -> None:
        """A comparison sign engraves verbatim (no closing delimiter)."""
        styled = parse_markup("3 < 5 and 5 > 3")
        assert styled.plain_text == "3 < 5 and 5 > 3"
        assert styled.has_markup is False

    def test_unregistered_tag_is_literal(self) -> None:
        """A bracketed name that is not registered engraves verbatim."""
        styled = parse_markup("a <xyz> b")
        assert styled.plain_text == "a <xyz> b"
        assert styled.has_markup is False

    def test_unregistered_close_is_literal(self) -> None:
        """A close tag for an unregistered name engraves verbatim."""
        styled = parse_markup("a </xyz> b")
        assert styled.plain_text == "a </xyz> b"
        assert styled.has_markup is False

    def test_open_without_close_is_literal(self) -> None:
        """An opening delimiter with no closing delimiter is literal."""
        styled = parse_markup("<i unclosed")
        assert styled.plain_text == "<i unclosed"
        assert styled.has_markup is False

    def test_tag_name_args_on_italic_is_literal(self) -> None:
        """``name:args`` is reserved: <i:0.3> is not markup today."""
        styled = parse_markup("<i:0.3>x")
        assert styled.plain_text == "<i:0.3>x"
        assert styled.has_markup is False


class TestParseItalic:
    """The shipped <i> tag."""

    def test_simple_span(self) -> None:
        """<i>...</i> yields a plain run, an italic run, and the tail."""
        styled = parse_markup("Hello <i>World</i>!")
        assert styled.runs == (
            Run("Hello ", PLAIN),
            Run("World", ITALIC),
            Run("!", PLAIN),
        )
        assert styled.plain_text == "Hello World!"
        assert styled.has_markup is True

    def test_span_at_edges(self) -> None:
        """A span covering the whole line yields one italic run."""
        styled = parse_markup("<i>AV</i>")
        assert styled.runs == (Run("AV", ITALIC),)
        assert styled.plain_text == "AV"

    def test_adjacent_spans_merge(self) -> None:
        """Two touching italic spans merge into one run."""
        styled = parse_markup("<i>a</i><i>b</i>")
        assert styled.runs == (Run("ab", ITALIC),)

    def test_repeated_spans(self) -> None:
        """Alternating spans keep their order and styles."""
        styled = parse_markup("<i>a</i>b<i>c</i>")
        assert [(r.text, r.style.italic) for r in styled.runs] == [
            ("a", True),
            ("b", False),
            ("c", True),
        ]

    def test_italic_flags_align_with_plain_text(self) -> None:
        """Per-character flags cover exactly the plain text."""
        styled = parse_markup("ab<i>cd</i>ef")
        assert styled.italic_flags == (
            False,
            False,
            True,
            True,
            False,
            False,
        )
        assert len(styled.italic_flags) == len(styled.plain_text)

    def test_span_spanning_whitespace(self) -> None:
        """A span crossing a space keeps the space inside the italic run."""
        styled = parse_markup("<i>foo bar</i>")
        assert styled.runs == (Run("foo bar", ITALIC),)
        assert styled.plain_text == "foo bar"

    def test_leftmost_match_wins(self) -> None:
        """The scan consumes the first closing delimiter after the opener."""
        # Brackets << >>: the opener is <<i>>, so the first >> closes it and
        # the trailing >> closes the span; the text between is exactly "x".
        styled = parse_markup("<<i>>x<</i>>", "<<", ">>")
        assert styled.plain_text == "x"
        assert styled.italic_flags == (True,)


class TestParseErrors:
    """Broken markup aborts loudly (MarkupError is a ValueError)."""

    def test_unclosed_tag(self) -> None:
        """An open tag never closed raises MarkupError."""
        with pytest.raises(MarkupError, match="Unclosed tag <i>"):
            parse_markup("<i>no end")

    def test_mismatched_close(self) -> None:
        """A close tag that is not the innermost opener raises MarkupError."""
        # <i> and <b> are both registered, so </b> closes nothing matching:
        # the innermost open tag is <i>, a genuine mismatch.
        with pytest.raises(MarkupError, match="does not match open tag"):
            parse_markup("<i>x</b>")

    def test_standalone_close(self) -> None:
        """A registered close tag with no open tag raises MarkupError."""
        with pytest.raises(MarkupError, match="no matching open tag"):
            parse_markup("a </i> b")

    def test_markup_error_is_value_error(self) -> None:
        """Callers guarding on ValueError keep working."""
        assert issubclass(MarkupError, ValueError)

    def test_identical_delimiters_rejected(self) -> None:
        """Identical delimiters cannot distinguish open from close."""
        with pytest.raises(ValueError, match="must differ"):
            parse_markup("x", "<", "<")

    def test_empty_delimiter_rejected(self) -> None:
        """An empty delimiter is a programming error."""
        with pytest.raises(ValueError, match="non-empty"):
            parse_markup("x", "", ">")


class TestMultiCharBrackets:
    """html_brackets: first half left, second half right."""

    def test_split_brackets(self) -> None:
        """The pair splits into its halves."""
        assert split_brackets("<>") == ("<", ">")
        assert split_brackets("<<>>") == ("<<", ">>")
        assert split_brackets("\u00ab\u00bb") == ("\u00ab", "\u00bb")
        assert split_brackets("[[]]") == ("[[", "]]")

    def test_split_brackets_odd_length(self) -> None:
        """An odd length is rejected (the schema is the real gate)."""
        with pytest.raises(ValueError, match="even number"):
            split_brackets("<>x")

    def test_guillemets_parse(self) -> None:
        """A single-character multi-byte pair works."""
        styled = parse_markup("\u00abi\u00bbx\u00ab/i\u00bb", "\u00ab", "\u00bb")
        assert styled.plain_text == "x"
        assert styled.italic_flags == (True,)
        mixed = parse_markup("a\u00abi\u00bbx\u00ab/i\u00bb", "\u00ab", "\u00bb")
        assert mixed.plain_text == "ax"
        assert mixed.italic_flags == (False, True)

    def test_chevrons_are_literal_under_guillemets(self) -> None:
        """The motivating case: chevron data is untouched under other pairs."""
        styled = parse_markup("<i>not markup</i>", "\u00ab", "\u00bb")
        assert styled.plain_text == "<i>not markup</i>"
        assert styled.has_markup is False

    def test_double_angle_pair(self) -> None:
        """A two-character-per-side pair works."""
        styled = parse_markup("[[i]]slant[[/i]]", "[[", "]]")
        assert styled.plain_text == "slant"
        assert styled.italic_flags == (True,) * 5


class TestSerialize:
    """serialize_runs / serialize_word round-trip through parse_markup."""

    def test_round_trip_simple(self) -> None:
        """Runs rebuild the raw markup and re-parse identically."""
        styled = parse_markup("Hello <i>World</i>!")
        raw = serialize_runs(styled.runs)
        assert raw == "Hello <i>World</i>!"
        assert parse_markup(raw).runs == styled.runs

    def test_round_trip_repeated_spans(self) -> None:
        """Alternating spans round-trip."""
        styled = parse_markup("<i>a</i>b<i>c</i>")
        assert parse_markup(serialize_runs(styled.runs)).runs == styled.runs

    def test_plain_runs_emit_no_tags(self) -> None:
        """A plain line serializes to bare text."""
        assert serialize_runs([Run("plain", PLAIN)]) == "plain"

    def test_serialize_uses_configured_brackets(self) -> None:
        """The emitted delimiters follow the caller's pair."""
        styled = parse_markup("\u00abi\u00bbx\u00ab/i\u00bb", "\u00ab", "\u00bb")
        assert serialize_runs(styled.runs, "\u00ab", "\u00bb") == "\u00abi\u00bbx\u00ab/i\u00bb"

    def test_serialize_word_uniform(self) -> None:
        """A style-uniform word serializes with one pair of tags."""
        styled = parse_markup("<i>foo</i>")
        (word,) = split_styled_words(styled)
        assert serialize_word(word) == "<i>foo</i>"

    def test_serialize_word_mixed_pieces(self) -> None:
        """A multi-piece word tags each piece."""
        styled = parse_markup("a<i>b</i>c")
        (word,) = split_styled_words(styled)
        assert word.text == "abc"
        assert serialize_word(word) == "a<i>b</i>c"


class TestStyledWords:
    """The word partition the line-content reflow consumes."""

    def test_plain_words(self) -> None:
        """A plain line splits on whitespace, all pieces plain."""
        styled = parse_markup("alpha beta  gamma")
        words = split_styled_words(styled)
        assert [w.text for w in words] == ["alpha", "beta", "gamma"]
        assert all(w.style == PLAIN for w in words)

    def test_span_inside_word_splits_pieces(self) -> None:
        """A span crossing a word boundary yields multiple pieces."""
        styled = parse_markup("a<i>b</i>c")
        words = split_styled_words(styled)
        assert len(words) == 1
        assert [(p.text, p.style.italic) for p in words[0].pieces] == [
            ("a", False),
            ("b", True),
            ("c", False),
        ]

    def test_span_across_words(self) -> None:
        """A span covering several words styles each of them."""
        styled = parse_markup("<i>foo bar</i> baz")
        words = split_styled_words(styled)
        assert [(w.text, w.style.italic) for w in words] == [
            ("foo", True),
            ("bar", True),
            ("baz", False),
        ]

    def test_blank_line_yields_no_words(self) -> None:
        """Whitespace-only text has no words (the reflow's scope guard)."""
        assert split_styled_words(parse_markup("   ")) == ()


class TestStyledWordsWithSeparators:
    """The word + separator-style partition the reflow rebuild consumes."""

    def test_plain_separators_are_plain(self) -> None:
        """A plain line yields PLAIN separators between its words."""
        words, separators = split_styled_words_with_separators(parse_markup("a b c"))
        assert [w.text for w in words] == ["a", "b", "c"]
        assert separators == (PLAIN, PLAIN)

    def test_span_covering_a_space_styles_the_separator(self) -> None:
        """``<u>foo bar</u>`` styles the space, so the rebuild stays continuous."""
        words, separators = split_styled_words_with_separators(parse_markup("<u>foo bar</u>"))
        assert [w.text for w in words] == ["foo", "bar"]
        assert separators == (UNDERLINE,)

    def test_plain_span_between_words_is_plain_separator(self) -> None:
        """Two separately-styled words share a PLAIN separator."""
        words, separators = split_styled_words_with_separators(
            parse_markup("<i>foo</i> <u>bar</u>")
        )
        assert [w.style for w in words] == [ITALIC, UNDERLINE]
        assert separators == (PLAIN,)

    def test_trailing_whitespace_is_dropped(self) -> None:
        """Trailing whitespace yields no trailing separator (one per junction)."""
        words, separators = split_styled_words_with_separators(parse_markup("a b  "))
        assert [w.text for w in words] == ["a", "b"]
        assert separators == (PLAIN,)

    def test_leading_whitespace_is_dropped(self) -> None:
        """Leading whitespace is skipped; the first word has no separator."""
        words, separators = split_styled_words_with_separators(parse_markup("  a b"))
        assert [w.text for w in words] == ["a", "b"]
        assert separators == (PLAIN,)

    def test_blank_line_yields_no_words(self) -> None:
        """Whitespace-only text yields no words and no separators."""
        assert split_styled_words_with_separators(parse_markup("   ")) == ((), ())

    def test_single_word_has_no_separator(self) -> None:
        """One word yields an empty separator tuple."""
        words, separators = split_styled_words_with_separators(parse_markup("<i>x</i>"))
        assert [w.text for w in words] == ["x"]
        assert separators == ()


class TestStyleBoundaries:
    """The reflow's scope-guard metric."""

    def test_plain_line_has_no_boundaries(self) -> None:
        """A plain line never changes style."""
        assert style_boundaries(parse_markup("plain text")) == ()

    def test_boundaries_at_span_edges(self) -> None:
        """Boundaries sit at the span's start and end indices."""
        styled = parse_markup("ab<i>cd</i>ef")
        assert style_boundaries(styled) == (2, 4)

    def test_span_inside_word_boundary(self) -> None:
        """A boundary with no whitespace between is an in-word split."""
        styled = parse_markup("a<i>b</i>c")
        boundaries = style_boundaries(styled)
        plain = styled.plain_text
        assert boundaries == (1, 2)
        assert not plain[boundaries[0] - 1].isspace()
        assert not plain[boundaries[0]].isspace()


class TestTagRegistry:
    """The framework's extension point."""

    def test_italic_is_registered(self) -> None:
        """The shipped tag is present and idempotent."""
        spec = TAG_REGISTRY["i"]
        assert spec.apply(PLAIN, "") == ITALIC
        assert spec.apply(ITALIC, "") == ITALIC
        assert spec.accepts_args is False

    def test_register_tag_rejects_bad_name(self) -> None:
        """Tag names are ASCII letters only."""
        with pytest.raises(ValueError, match="ASCII letters"):
            register_tag("bad-name", lambda style, arg: style)

    def test_register_tag_round_trip(self) -> None:
        """A registered tag parses like the shipped one.

        Serialization emits the *first* registry tag whose ``apply`` is
        active (registry order fixes nesting), so a tag mapping to the same
        style as ``i`` serializes as ``<i>`` -- the round-trip of the parse
        is what the reflow relies on.
        """
        original = dict(TAG_REGISTRY)
        try:
            register_tag("zz", lambda style, arg: Style(italic=True))
            styled = parse_markup("<zz>x</zz>")
            assert styled.plain_text == "x"
            assert styled.italic_flags == (True,)
            assert parse_markup(serialize_runs(styled.runs)).runs == styled.runs
        finally:
            TAG_REGISTRY.clear()
            TAG_REGISTRY.update(original)


class TestFrameworkEdges:
    """Branches only reachable through the registry / direct run lists."""

    def test_empty_runs_are_dropped_by_the_merger(self) -> None:
        """An empty run never survives into the parsed runs."""
        styled = parse_markup("<i></i>")
        assert styled.runs == ()
        assert styled.plain_text == ""
        assert styled.has_markup is True

    def test_empty_runs_are_skipped_when_serializing(self) -> None:
        """An empty run contributes no text and no tags."""
        assert serialize_runs([Run("", PLAIN), Run("x", ITALIC)]) == "<i>x</i>"

    def test_mismatched_registered_close_raises(self) -> None:
        """A close tag for a *different registered* tag is a hard error.

        With only ``<i>`` shipped, ``</b>`` is literal; registering a second
        tag makes the pair markup, so the mismatch branch fires.
        """
        original = dict(TAG_REGISTRY)
        try:
            register_tag("b", lambda style: style)
            with pytest.raises(MarkupError, match="does not match open tag"):
                parse_markup("<i>x</b>")
        finally:
            TAG_REGISTRY.clear()
            TAG_REGISTRY.update(original)


def test_merge_runs_drops_empty_runs_directly() -> None:
    """The merger drops empties and merges equal-style neighbours."""
    from plt_optimizer.generate.markup import _merge_runs

    assert _merge_runs([Run("", PLAIN), Run("a", PLAIN), Run("b", PLAIN)]) == (Run("ab", PLAIN),)


class TestParseUnderline:
    """The shipped <u> tag."""

    def test_simple_span(self) -> None:
        """<u>...</u> yields a plain run, an underlined run, and the tail."""
        styled = parse_markup("Hello <u>World</u>!")
        assert styled.runs == (
            Run("Hello ", PLAIN),
            Run("World", UNDERLINE),
            Run("!", PLAIN),
        )
        assert styled.plain_text == "Hello World!"
        assert styled.has_markup is True

    def test_underline_flags_align_with_plain_text(self) -> None:
        """Per-character underline flags cover exactly the plain text."""
        styled = parse_markup("ab<u>cd</u>ef")
        assert styled.underline_flags == (False, False, True, True, False, False)
        assert len(styled.underline_flags) == len(styled.plain_text)

    def test_italic_flags_stay_plain_on_an_underline(self) -> None:
        """The two styles are independent fields of Style."""
        styled = parse_markup("<u>cd</u>")
        assert styled.underline_flags == (True, True)
        assert styled.italic_flags == (False, False)

    def test_underline_span_spanning_whitespace(self) -> None:
        """A span crossing a space keeps the space inside the underlined run."""
        styled = parse_markup("<u>foo bar</u>")
        assert styled.runs == (Run("foo bar", UNDERLINE),)
        assert styled.underline_flags == (True,) * 7


class TestParseNested:
    """Nested markup: the parser's stack composes every registered style."""

    def test_underline_inside_italic(self) -> None:
        """<i>...<u>...</u>...</i> styles the inner span with both flags."""
        styled = parse_markup("<i>nested <u>markup</u> tail</i>")
        assert styled.plain_text == "nested markup tail"
        assert styled.italic_flags == (True,) * len(styled.plain_text)
        # 'nested ' (7 chars) plain italic, 'markup' (6) italic+underline,
        # ' tail' (5) plain italic again.
        assert styled.underline_flags == (False,) * 7 + (True,) * 6 + (False,) * 5

    def test_italic_inside_underline(self) -> None:
        """The nesting order is symmetric: <u>...<i>...</i>...</u> composes."""
        styled = parse_markup("<u>aa<i>bb</i>cc</u>")
        assert styled.plain_text == "aabbcc"
        assert styled.underline_flags == (True,) * 6
        assert styled.italic_flags == (False, False, True, True, False, False)

    def test_runs_carry_the_composed_style(self) -> None:
        """A nested span's run carries both flags on one Style."""
        styled = parse_markup("<i>a<u>b</u>c</i>")
        assert [(r.text, r.style) for r in styled.runs] == [
            ("a", Style(italic=True)),
            ("b", Style(italic=True, underline=True)),
            ("c", Style(italic=True)),
        ]

    def test_mismatched_nested_close_raises(self) -> None:
        """Closing <i> while <u> is innermost is a hard error."""
        with pytest.raises(MarkupError, match="does not match open tag"):
            parse_markup("<i>x<u>y</i></u>")

    def test_unclosed_nested_span_raises(self) -> None:
        """An inner span closed by the outer tag's closer is a hard error."""
        with pytest.raises(MarkupError, match="does not match open tag"):
            parse_markup("<i>x<u>y</i>")
        # A line ending inside the inner span reports the innermost opener.
        with pytest.raises(MarkupError, match="Unclosed tag <u>"):
            parse_markup("<i>x<u>y")

    def test_nested_serialization_round_trips(self) -> None:
        """The registry order fixes the nesting serialization recovers."""
        styled = parse_markup("<i>nested <u>markup</u> tail</i>")
        raw = serialize_runs(styled.runs)
        assert parse_markup(raw).runs == styled.runs
        assert parse_markup(raw).plain_text == styled.plain_text

    def test_nested_word_serialization_round_trips(self) -> None:
        """A nested word re-parses to the same pieces (the reflow contract)."""
        styled = parse_markup("<i>ab<u>cd</u></i>")
        (word,) = split_styled_words(styled)
        reparsed = parse_markup(serialize_word(word))
        assert reparsed.plain_text == word.text
        assert reparsed.underline_flags == styled.underline_flags
        assert reparsed.italic_flags == styled.italic_flags


class TestUnderlineRegistry:
    """The <u> registry entry."""

    def test_underline_is_registered(self) -> None:
        """The shipped tag is present, idempotent, and takes no args."""
        spec = TAG_REGISTRY["u"]
        assert spec.apply(PLAIN, "") == UNDERLINE
        assert spec.apply(UNDERLINE, "") == UNDERLINE
        # Applying <u> inside <i> composes (idempotent per field).
        assert spec.apply(ITALIC, "") == Style(italic=True, underline=True)
        assert spec.accepts_args is False

    def test_italic_and_underline_are_distinct_tags(self) -> None:
        """Registry order (i, u, b, scale, sup, sub) fixes serialization nesting."""
        assert list(TAG_REGISTRY) == ["i", "u", "b", "scale", "sup", "sub"]


class TestParseBold:
    """The <b> tag: a bold run over the plain text."""

    def test_bold_run_splits_the_line(self) -> None:
        """plain <b>X</b> plain yields plain/bold/plain runs."""
        styled = parse_markup("plain <b>X</b> plain")
        assert styled.plain_text == "plain X plain"
        assert styled.has_markup is True
        assert [(r.text, r.style.bold) for r in styled.runs] == [
            ("plain ", False),
            ("X", True),
            (" plain", False),
        ]

    def test_bold_flags_align_with_plain_text(self) -> None:
        """bold_flags is one flag per plain_text character (spaces included)."""
        styled = parse_markup("a<b>bc</b>d")
        assert styled.plain_text == "abcd"
        assert styled.bold_flags == (False, True, True, False)

    def test_whole_line_bold(self) -> None:
        """A fully bold line is one bold run."""
        styled = parse_markup("<b>ALL</b>")
        assert styled.runs == (Run("ALL", BOLD),)
        assert styled.bold_flags == (True, True, True)

    def test_bold_italic_compose(self) -> None:
        """<i>...<b>...</b>...</i> carries both flags inside the bold run."""
        styled = parse_markup("<i>a<b>b</b>c</i>")
        assert styled.plain_text == "abc"
        assert [(r.text, r.style.italic, r.style.bold) for r in styled.runs] == [
            ("a", True, False),
            ("b", True, True),
            ("c", True, False),
        ]

    def test_bold_underline_compose(self) -> None:
        """<b><u>...</u></b> carries bold + underline on the run."""
        styled = parse_markup("<b><u>pay</u></b>")
        assert styled.plain_text == "pay"
        assert all(r.style.bold and r.style.underline for r in styled.runs)

    def test_bold_round_trips_through_serialize(self) -> None:
        """serialize_runs rebuilds the raw markup from the styles."""
        styled = parse_markup("plain <b>X</b> plain")
        assert serialize_runs(styled.runs) == "plain <b>X</b> plain"

    def test_bold_round_trips_nested_in_italic(self) -> None:
        """A nested bold/italic line re-parses to the same runs (registry order)."""
        styled = parse_markup("<i>a<b>b</b>c</i>")
        raw = serialize_runs(styled.runs)
        # The serializer emits per-run tags; re-parsing recovers the styles.
        assert parse_markup(raw).runs == styled.runs
        assert parse_markup(raw).plain_text == "abc"


class TestBoldRegistry:
    """The <b> registry entry."""

    def test_bold_is_registered(self) -> None:
        """The shipped tag is present, idempotent, and takes no args."""
        spec = TAG_REGISTRY["b"]
        assert spec.apply(PLAIN, "") == BOLD
        assert spec.apply(BOLD, "") == BOLD
        # Applying <b> inside <i> composes (idempotent per field).
        assert spec.apply(ITALIC, "") == Style(italic=True, bold=True)
        assert spec.accepts_args is False

    def test_bold_args_form_is_literal(self) -> None:
        """``name:args`` stays reserved: <b:2> is not markup today."""
        styled = parse_markup("<b:2>x")
        assert styled.plain_text == "<b:2>x"
        assert styled.has_markup is False


SCALED_HALF = Style(scale=0.5)


class TestScaleRegistry:
    """The <scale> registry entry (the first args tag)."""

    def test_scale_is_registered_with_args(self) -> None:
        """The tag is registered, accepts args, and is registered last (of the
        mandatory-arg tags; sup/sub joined after it)."""
        spec = TAG_REGISTRY["scale"]
        assert spec.accepts_args is True
        assert list(TAG_REGISTRY) == ["i", "u", "b", "scale", "sup", "sub"]
        assert spec.apply(PLAIN, "0.5") == SCALED_HALF
        # Set-to-value: re-applying with the same argument is idempotent.
        assert spec.apply(SCALED_HALF, "0.5") == SCALED_HALF
        # A new argument replaces the active one.
        assert spec.apply(SCALED_HALF, "1.5") == Style(scale=1.5)
        # Applying <scale> inside <i> composes.
        assert spec.apply(ITALIC, "0.5") == Style(italic=True, scale=0.5)

    def test_is_active_predicate(self) -> None:
        """The tag is active exactly when the scale differs from 1.0."""
        spec = TAG_REGISTRY["scale"]
        assert spec.is_active(PLAIN) is False
        assert spec.is_active(SCALED_HALF) is True

    def test_arg_serializer_round_trip(self) -> None:
        """The arg serializer emits the canonical value the parser accepts."""
        spec = TAG_REGISTRY["scale"]
        assert spec.arg is not None
        assert spec.arg(SCALED_HALF) == "0.5"
        assert spec.arg(Style(scale=1.5)) == "1.5"
        assert spec.apply(PLAIN, spec.arg(SCALED_HALF)) == SCALED_HALF

    def test_register_tag_requires_an_arg_serializer(self) -> None:
        """An args tag without an arg serializer is a registration error."""
        with pytest.raises(ValueError, match="arg serializer"):
            register_tag("zz", lambda style, arg: style, accepts_args=True)


class TestScaleParse:
    """Parsing the <scale:N> tag."""

    def test_scale_run_and_values(self) -> None:
        """The run carries the factor and scale_values align with plain_text."""
        styled = parse_markup("1<scale:0.5>st</scale>")
        assert styled.plain_text == "1st"
        assert styled.has_markup is True
        assert styled.runs == (Run("1", PLAIN), Run("st", SCALED_HALF))
        assert styled.scale_values == (1.0, 0.5, 0.5)

    def test_bare_scale_tag_is_literal(self) -> None:
        """A value is mandatory: <scale> without an argument engraves literally."""
        styled = parse_markup("a <scale>x")
        assert styled.plain_text == "a <scale>x"
        assert styled.has_markup is False

    def test_space_in_argument_is_literal(self) -> None:
        """``<scale: 0.5>`` never matches the tag body: literal engraving."""
        styled = parse_markup("<scale: 0.5>x")
        assert styled.plain_text == "<scale: 0.5>x"
        assert styled.has_markup is False

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            ("<scale:abc>x</scale>", "is not a number"),
            ("<scale:>x</scale>", "is not a number"),
            ("<scale:0.1>x</scale>", "outside the supported range"),
            ("<scale:5>x</scale>", "outside the supported range"),
            ("<scale:-1>x</scale>", "outside the supported range"),
        ],
    )
    def test_bad_argument_is_a_hard_error(self, raw: str, reason: str) -> None:
        """A bad argument aborts loudly (MarkupError, fail-loud contract)."""
        with pytest.raises(MarkupError, match=reason):
            parse_markup(raw)

    def test_bounds_are_inclusive(self) -> None:
        """The [0.2, 4.0] endpoints parse."""
        assert parse_markup("<scale:0.2>x</scale>").scale_values == (0.2,)
        assert parse_markup("<scale:4>x</scale>").scale_values == (4.0,)

    def test_scale_inside_italic_composes(self) -> None:
        """A scale span nested in italic carries both styles."""
        styled = parse_markup("<i>it <scale:0.5>mix</scale></i>")
        assert styled.italic_flags == (True, True, True, True, True, True)
        assert styled.scale_values == (1.0, 1.0, 1.0, 0.5, 0.5, 0.5)


class TestScaleSerialize:
    """The serialize round-trip keeps the argument (the reflow depends on it)."""

    @pytest.mark.parametrize(
        "raw",
        [
            "1<scale:0.5>st</scale> 2<scale:1.5>nd</scale>",
            "<scale:0.5>a<scale:1.5>b</scale>c</scale>",
            "<i>it </i><i><scale:0.5>mix</scale></i>",
            "<b>bd <scale:0.75>small</scale></b>",
        ],
    )
    def test_round_trip_is_lossless(self, raw: str) -> None:
        """Serialization re-emits the argument and re-parses to the same runs."""
        styled = parse_markup(raw)
        serialized = serialize_runs(styled.runs)
        assert parse_markup(serialized).runs == styled.runs

    def test_nested_scale_restores_the_outer_value(self) -> None:
        """The close of an inner scale tag restores the outer factor."""
        styled = parse_markup("<scale:0.5>a<scale:1.5>b</scale>c</scale>")
        assert styled.scale_values == (0.5, 1.5, 0.5)
        # Serialization flattens the nesting (each span gets its own tag pair),
        # which re-parses to the identical per-character factors.
        reparsed = parse_markup(serialize_runs(styled.runs))
        assert reparsed.scale_values == (0.5, 1.5, 0.5)

    def test_flag_tag_serialization_is_unchanged(self) -> None:
        """The i/u/b round-trip stays exactly the historical text."""
        raw = "<i>it</i> <b>bd</b> <u>un</u>"
        assert serialize_runs(parse_markup(raw).runs) == raw


class TestScaleWords:
    """The reflow's word tokens keep the scale argument."""

    def test_serialize_word_keeps_the_argument(self) -> None:
        """A styled word token round-trips with its factor."""
        styled = parse_markup("big <scale:0.5>small</scale>")
        words = split_styled_words(styled)
        assert [word.text for word in words] == ["big", "small"]
        assert serialize_word(words[1]) == "<scale:0.5>small</scale>"
        assert serialize_word(words[0]) == "big"

    def test_style_boundaries_see_the_scale_change(self) -> None:
        """A scale span's edges are style boundaries (the reflow's guard)."""
        styled = parse_markup("big<scale:0.5>small</scale>")
        assert style_boundaries(styled) == (3,)


SUP = Style(sup_sub=SupSub(SUP_DIRECTION))
SUP_03 = Style(sup_sub=SupSub(SUP_DIRECTION, 0.3))
SUB = Style(sup_sub=SupSub(SUB_DIRECTION))
# The bare SupSub values the per-character properties yield (the properties
# expand styles to their sup_sub field, so tests compare against these).
SUP_SS = SupSub(SUP_DIRECTION)
SUP_SS_03 = SupSub(SUP_DIRECTION, 0.3)
SUB_SS = SupSub(SUB_DIRECTION)


class TestSupSubRegistry:
    """The <sup>/<sub> registry entries (the first optional-arg tags)."""

    def test_sup_sub_are_registered_last(self) -> None:
        """Both tags accept an optional arg and are registered after scale."""
        assert list(TAG_REGISTRY) == ["i", "u", "b", "scale", "sup", "sub"]
        for name in ("sup", "sub"):
            spec = TAG_REGISTRY[name]
            assert spec.accepts_args is True
            assert spec.args_optional is True

    def test_bare_form_derives_and_is_idempotent(self) -> None:
        """The bare form applies with shift None; re-applying is a no-op."""
        sup = TAG_REGISTRY["sup"]
        assert sup.apply(PLAIN, "") == SUP
        assert sup.apply(SUP, "") == SUP
        assert sup.apply(SUP_03, "") == SUP
        # A new argument replaces the active shift.
        assert sup.apply(SUP_03, "0.5") == Style(sup_sub=SupSub(SUP_DIRECTION, 0.5))
        # Applying <sup> inside <i> composes.
        assert sup.apply(ITALIC, "") == Style(italic=True, sup_sub=SupSub(SUP_DIRECTION))

    def test_is_active_is_direction_specific(self) -> None:
        """Each tag is active exactly for its own direction (mutual exclusion)."""
        assert TAG_REGISTRY["sup"].is_active(PLAIN) is False
        assert TAG_REGISTRY["sup"].is_active(SUP) is True
        assert TAG_REGISTRY["sup"].is_active(SUP_03) is True
        assert TAG_REGISTRY["sup"].is_active(SUB) is False
        assert TAG_REGISTRY["sub"].is_active(SUB) is True
        assert TAG_REGISTRY["sub"].is_active(SUP) is False

    def test_arg_serializer_round_trip(self) -> None:
        """The bare form serializes to '' (bare tag); the arg form to the value."""
        sup = TAG_REGISTRY["sup"]
        assert sup.arg is not None
        assert sup.arg(SUP) == ""
        assert sup.arg(SUP_03) == "0.3"
        # The other direction's style serializes empty (its is_active is False,
        # so serialize_runs never asks -- the guard keeps it total).
        assert sup.arg(SUB) == ""
        assert sup.apply(PLAIN, sup.arg(SUP_03)) == SUP_03

    def test_args_optional_registration_shape(self) -> None:
        """args_optional is stored on the spec (a mandatory-arg tag keeps False)."""
        assert TAG_REGISTRY["scale"].args_optional is False
        assert TAG_REGISTRY["i"].args_optional is False


class TestSupSubParse:
    """Parsing the <sup>/<sub> tags (bare and argumented forms)."""

    def test_bare_sup_run_and_values(self) -> None:
        """The run carries the shift and sup_sub_values align with plain_text."""
        styled = parse_markup("1<sup>st</sup>")
        assert styled.plain_text == "1st"
        assert styled.has_markup is True
        assert styled.runs == (Run("1", PLAIN), Run("st", SUP))
        assert styled.sup_sub_values == (None, SUP_SS, SUP_SS)

    def test_bare_tags_are_markup(self) -> None:
        """Unlike <scale>, a bare <sup>/<sub> IS markup (args_optional)."""
        styled = parse_markup("a <sup>x</sup> <sub>y</sub>")
        assert styled.plain_text == "a x y"
        assert styled.has_markup is True
        assert styled.sup_sub_values == (None, None, SUP_SS, None, SUB_SS)

    def test_argumented_shift(self) -> None:
        """<sup:0.3> carries the explicit shift; the bare form carries None."""
        styled = parse_markup("1<sup:0.3>st</sup>")
        assert styled.sup_sub_values == (None, SUP_SS_03, SUP_SS_03)

    def test_space_in_argument_is_literal(self) -> None:
        """``<sup: 0.3>`` never matches the tag body: literal engraving."""
        styled = parse_markup("<sup: 0.3>x")
        assert styled.plain_text == "<sup: 0.3>x"
        assert styled.has_markup is False

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            ("<sup:abc>x</sup>", "is not a number"),
            ("<sub:abc>x</sub>", "is not a number"),
            ("<sup:2>x</sup>", "outside the supported range"),
            ("<sup:-1.5>x</sup>", "outside the supported range"),
            ("<sub:-2>x</sub>", "outside the supported range"),
        ],
    )
    def test_bad_argument_is_a_hard_error(self, raw: str, reason: str) -> None:
        """A bad argument aborts loudly (MarkupError, fail-loud contract)."""
        with pytest.raises(MarkupError, match=reason):
            parse_markup(raw)

    def test_bounds_are_inclusive(self) -> None:
        """The [-1.0, 1.0] endpoints parse."""
        assert parse_markup("<sup:-1>x</sup>").sup_sub_values[0] == SupSub(
            SUP_DIRECTION, -1.0
        )
        assert parse_markup("<sub:1>x</sub>").sup_sub_values[0] == SupSub(
            SUB_DIRECTION, 1.0
        )

    def test_mismatched_close_is_a_hard_error(self) -> None:
        """<sup>...</sub> aborts (the close must match the innermost open)."""
        with pytest.raises(MarkupError, match="does not match open tag"):
            parse_markup("<sup>x</sub>")

    def test_nested_sup_sub_restores_the_outer(self) -> None:
        """<sup>a<sub>b</sub>c</sup> restores the superscript for c."""
        styled = parse_markup("<sup>a<sub>b</sub>c</sup>")
        assert styled.sup_sub_values == (SUP_SS, SUB_SS, SUP_SS)

    def test_sup_sub_inside_italic_composes(self) -> None:
        """A shift span nested in italic carries both styles."""
        styled = parse_markup("<i>it <sup>mix</sup></i>")
        assert styled.italic_flags == (True,) * 6
        assert styled.sup_sub_values == (None, None, None, SUP_SS, SUP_SS, SUP_SS)

    def test_sup_sub_with_scale_composes(self) -> None:
        """A scale span nested in sup carries both values."""
        styled = parse_markup("<sup>a<scale:0.5>b</scale></sup>")
        assert styled.scale_values == (1.0, 0.5)
        assert styled.sup_sub_values == (SUP_SS, SUP_SS)


class TestSupSubSerialize:
    """The serialize round-trip keeps the bare/argumented forms distinct."""

    @pytest.mark.parametrize(
        "raw",
        [
            "1<sup>st</sup> 2<sub>nd</sub>",
            "1<sup:0.3>st</sup>",
            "<sup>a<sub>b</sub>c</sup>",
            "<i>it </i><i><sup>mix</sup></i>",
            "<b>bd <sup>small</sup></b>",
            "<scale:0.5>a<sup>b</sup></scale>",
        ],
    )
    def test_round_trip_is_lossless(self, raw: str) -> None:
        """Serialization re-emits the bare/arg form and re-parses identically."""
        styled = parse_markup(raw)
        serialized = serialize_runs(styled.runs)
        assert parse_markup(serialized).runs == styled.runs

    def test_bare_form_serializes_bare(self) -> None:
        """A derived-shift run emits the bare tag (no ':arg')."""
        assert serialize_runs(parse_markup("1<sup>st</sup>").runs) == "1<sup>st</sup>"
        assert serialize_runs(parse_markup("<sub>x</sub>").runs) == "<sub>x</sub>"

    def test_nested_sup_sub_flattens(self) -> None:
        """The nesting flattens into per-span pairs re-parsing to the same shifts."""
        styled = parse_markup("<sup>a<sub>b</sub>c</sup>")
        reparsed = parse_markup(serialize_runs(styled.runs))
        assert reparsed.sup_sub_values == (SUP_SS, SUB_SS, SUP_SS)

    def test_flag_tag_serialization_is_unchanged(self) -> None:
        """The i/u/b/scale round-trip stays exactly the historical text."""
        raw = "<i>it</i> <b>bd</b> <u>un</u> <scale:0.5>sc</scale>"
        assert serialize_runs(parse_markup(raw).runs) == raw


class TestSupSubWords:
    """The reflow's word tokens keep the sup/sub form."""

    def test_serialize_word_keeps_the_form(self) -> None:
        """A styled word token round-trips bare and argumented."""
        styled = parse_markup("x <sup>up</sup> y<sup:0.3>z</sup>")
        words = split_styled_words(styled)
        assert [word.text for word in words] == ["x", "up", "yz"]
        assert serialize_word(words[1]) == "<sup>up</sup>"
        assert serialize_word(words[0]) == "x"
        # The last word mixes a plain piece with the shifted piece.
        assert serialize_word(words[2]) == "y<sup:0.3>z</sup>"

    def test_style_boundaries_see_the_shift_change(self) -> None:
        """A shift span's edges are style boundaries (the reflow's guard)."""
        styled = parse_markup("big<sup>small</sup>")
        assert style_boundaries(styled) == (3,)
