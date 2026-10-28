"""Tests for the inline markup parser (:mod:`plt_optimizer.generate.markup`).

Covers the framework's contract: literal-first scanning, configurable
multi-character delimiters, hard errors on broken markup, the run/word
partitions the renderers and the reflow consume, and the serialize round-trip.
"""

from __future__ import annotations

from typing import List

import pytest

from plt_optimizer.generate.markup import (
    MAX_BUFFER_ARG,
    MIN_BUFFER_ARG,
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

    def test_close_of_a_never_opened_tag(self) -> None:
        """A close tag whose name was never opened raises MarkupError."""
        # <i> and <b> are both registered, so </b> closes nothing: <b> was
        # never opened, which stays a hard error (crossing spans only
        # relax closes whose name IS open somewhere on the stack).
        with pytest.raises(MarkupError, match="never opened"):
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
        tag makes the pair markup, so the never-opened branch fires.
        """
        original = dict(TAG_REGISTRY)
        try:
            register_tag("b", lambda style: style)
            with pytest.raises(MarkupError, match="never opened"):
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

    def test_crossing_nested_close_is_legal(self) -> None:
        """Closing <i> while <u> is innermost crosses the spans (HTML-style).

        Each tag covers its own open..close range, so the style of a
        character is the intersection of the spans covering it.
        """
        styled = parse_markup("<i>x<u>y</i></u>")
        # 'x' italic; 'y' italic + underline (the </i> ends the italic, the
        # </u> ends the underline).
        assert styled.italic_flags == (True, True)
        assert styled.underline_flags == (False, True)

    def test_unclosed_nested_span_raises(self) -> None:
        """A span left open at end-of-line is a hard error."""
        # The crossing </i> closes <i>; <u> stays open to end of line.
        with pytest.raises(MarkupError, match="Unclosed tag <u>"):
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


class TestCrossingSpans:
    """Overlapping (crossing) markup spans: per-character style intersection.

    A close tag closes the most recent open tag with that name, which need
    not be the innermost one; each tag covers its own open..close range, so
    a character's style is the intersection of the spans covering it
    (HTML-style). ``<s>Always <sc>vacuum</s> charging line</sc>`` is the
    motivating example from the TODO.
    """

    def test_todo_example_intersects_the_spans(self) -> None:
        """'Always ' struck, 'vacuum' struck+small caps, tail small caps."""
        styled = parse_markup("<s>Always <sc>vacuum</s> charging line</sc>")
        assert styled.plain_text == "Always vacuum charging line"
        # 'Always vacuum' (13 chars) struck; 'vacuum charging line' (20)
        # in small caps.
        assert styled.strikethrough_flags == (True,) * 13 + (False,) * 14
        assert styled.small_caps_flags == (False,) * 7 + (True,) * 20
        assert styled.runs == (
            Run("Always ", Style(strikethrough=True)),
            Run("vacuum", Style(strikethrough=True, small_caps=True)),
            Run(" charging line", Style(small_caps=True)),
        )

    def test_crossing_close_restores_the_surrounding_span(self) -> None:
        """The text after a crossing close keeps the spans still open."""
        styled = parse_markup("<i>a<b>c</i>d</b>")
        # plain text 'acd': 'a' italic; 'c' italic+bold; 'd' bold only (the
        # </i> ended the italic, the replay keeps the bold open).
        assert styled.italic_flags == (True, True, False)
        assert styled.bold_flags == (False, True, True)

    def test_crossing_close_replays_value_tags(self) -> None:
        """A crossing close rebuilds <scale:N>/<sup> values by replay."""
        styled = parse_markup("<scale:0.5>a<i>b</scale>c</i>")
        # 'a' 0.5; 'b' 0.5+italic; 'c' italic only (the scale span ended).
        assert styled.scale_values == (0.5, 0.5, 1.0)
        assert styled.italic_flags == (False, True, True)

    def test_crossing_sup_sub_is_legal(self) -> None:
        """<sup>a<sub>b</sup>c</sub> crosses: sup ends, sub keeps running."""
        styled = parse_markup("<sup>a<sub>b</sup>c</sub>")
        # 'a' sup; 'b' sub (sub's apply overrides sup_sub); 'c' sub (the
        # crossing </sup> replays the open sub, so its shift persists).
        assert styled.sup_sub_values == (
            SupSub(SUP_DIRECTION),
            SupSub(SUB_DIRECTION),
            SupSub(SUB_DIRECTION),
        )

    def test_duplicate_open_closes_the_innermost_instance(self) -> None:
        """<i>a<i>b</i>c</i> closes the inner <i> at the first </i>."""
        styled = parse_markup("<i>a<i>b</i>c</i>")
        # The first </i> closes the inner open; the outer stays open across
        # 'c', so the whole line is italic and merges to one run.
        assert styled.italic_flags == (True, True, True)
        assert styled.plain_text == "abc"
        assert len(styled.runs) == 1

    def test_crossing_serializes_to_the_canonical_nesting(self) -> None:
        """serialize_runs emits the nested form; it re-parses identically."""
        styled = parse_markup("<s>Always <sc>vacuum</s> charging line</sc>")
        raw = serialize_runs(styled.runs)
        reparsed = parse_markup(raw)
        assert reparsed.runs == styled.runs
        assert reparsed.plain_text == styled.plain_text

    def test_crossing_word_tokens_round_trip(self) -> None:
        """A crossing boundary inside a word rebuilds via per-piece tokens."""
        styled = parse_markup("x <s>ab<sc>cd</s>ef</sc> y")
        words, separators = split_styled_words_with_separators(styled)
        assert [word.text for word in words] == ["x", "abcdef", "y"]
        # The reflow's join idiom: serialized words interleaved with the
        # separator styles (line_content._join_tokens).
        runs: List[Run] = []
        for index, word in enumerate(words):
            runs.extend(Run(piece.text, piece.style) for piece in word.pieces)
            if index < len(separators):
                runs.append(Run(" ", separators[index]))
        rebuilt = serialize_runs(runs)
        reparsed = parse_markup(rebuilt)
        assert reparsed.plain_text == styled.plain_text
        assert reparsed.strikethrough_flags == styled.strikethrough_flags
        assert reparsed.small_caps_flags == styled.small_caps_flags
        assert separators == (PLAIN, PLAIN)
    def test_crossing_with_custom_brackets(self) -> None:
        """Crossing spans parse under a non-default bracket pair."""
        raw = "«s»Always «sc»vacuum«/s» line«/sc»"
        styled = parse_markup(raw, "«", "»")
        assert styled.plain_text == "Always vacuum line"
        # 'Always vacuum' struck; 'vacuum line' in small caps.
        assert styled.strikethrough_flags == (True,) * 13 + (False,) * 5
        assert styled.small_caps_flags == (False,) * 7 + (True,) * 11

    def test_close_with_never_opened_name_inside_spans_raises(self) -> None:
        """A crossing close still requires its name to be open somewhere."""
        with pytest.raises(MarkupError, match="never opened"):
            parse_markup("<i>a<u>b</s>c</u></i>")


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
        """Registry order (i, u, b, scale, sup, sub, s, sc, f, c, oc, vc, tc, cb) fixes serialization nesting."""
        assert list(TAG_REGISTRY) == [
            "i",
            "u",
            "b",
            "scale",
            "sup",
            "sub",
            "s",
            "sc",
            "f",
            "c",
            "oc",
            "vc",
            "tc",
            "cb",
        ]


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
        assert list(TAG_REGISTRY) == [
            "i",
            "u",
            "b",
            "scale",
            "sup",
            "sub",
            "s",
            "sc",
            "f",
            "c",
            "oc",
            "vc",
            "tc",
            "cb",
        ]
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
        assert list(TAG_REGISTRY) == [
            "i",
            "u",
            "b",
            "scale",
            "sup",
            "sub",
            "s",
            "sc",
            "f",
            "c",
            "oc",
            "vc",
            "tc",
            "cb",
        ]
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
        """<sup>...</sub> aborts (</sub> closes a tag that was never opened)."""
        with pytest.raises(MarkupError, match="never opened"):
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


STRIKE = Style(strikethrough=True)


class TestParseStrikethrough:
    """The <s> tag: a struck run over the plain text."""

    def test_strikethrough_run_splits_the_line(self) -> None:
        """plain <s>X</s> plain yields plain/struck/plain runs."""
        styled = parse_markup("was <s>now</s> sold")
        assert styled.plain_text == "was now sold"
        assert styled.has_markup is True
        assert styled.strikethrough_flags == (
            False,
            False,
            False,
            False,
            True,
            True,
            True,
            False,
            False,
            False,
            False,
            False,
        )

    def test_bare_s_is_the_tag(self) -> None:
        """The bare tag is markup (no argument form ships)."""
        styled = parse_markup("<s>sale</s>")
        assert styled.plain_text == "sale"
        assert all(styled.strikethrough_flags)

    def test_bare_scale_style_is_untouched(self) -> None:
        """<s> never touches the other style fields."""
        styled = parse_markup("<s>x</s>")
        (run,) = styled.runs
        assert run.style == STRIKE
        assert run.style.italic is False
        assert run.style.underline is False
        assert run.style.bold is False

    def test_multi_run_strikethrough(self) -> None:
        """Two disjoint struck runs stay two runs."""
        styled = parse_markup("A<s>bb</s>C<s>dd</s>")
        assert styled.plain_text == "AbbCdd"
        assert styled.strikethrough_flags == (
            False,
            True,
            True,
            False,
            True,
            True,
        )

    def test_strikethrough_inside_italic_composes(self) -> None:
        """<s> inside <i> carries both flags."""
        styled = parse_markup("<i>nested <s>markup</s> tail</i>")
        assert styled.plain_text == "nested markup tail"
        # 'markup' carries italic + strikethrough; the rest italic only.
        styles = [style for run in styled.runs for style in (run.style,) * len(run.text)]
        assert styles[7] == Style(italic=True, strikethrough=True)
        assert styles[0] == ITALIC

    def test_unclosed_strikethrough_raises(self) -> None:
        """An unclosed <s> is a hard error (the fail-loud contract)."""
        with pytest.raises(MarkupError, match="Unclosed tag <s>"):
            parse_markup("<s>sale")

    def test_mismatched_close_raises(self) -> None:
        """</s> with no open <s> is a hard error (never opened)."""
        with pytest.raises(MarkupError, match="never opened"):
            parse_markup("<i>x</s>")

    def test_serialization_round_trips(self) -> None:
        """The strike tag re-emits and re-parses identically."""
        raw = "<s>strike</s> and <i><s>both</s></i>"
        styled = parse_markup(raw)
        assert serialize_runs(styled.runs) == raw
        assert parse_markup(raw).runs == styled.runs

    def test_nested_word_serialization_round_trips(self) -> None:
        """A struck word token round-trips (the reflow contract)."""
        styled = parse_markup("x <s>abc</s> y")
        words = split_styled_words(styled)
        assert [word.text for word in words] == ["x", "abc", "y"]
        assert serialize_word(words[1]) == "<s>abc</s>"
        reparsed = parse_markup(serialize_word(words[1]))
        assert reparsed.plain_text == "abc"
        assert all(reparsed.strikethrough_flags)

    def test_strike_span_covering_a_space_keeps_the_separator(self) -> None:
        """<s>foo bar</s> tokenizes with the struck separator (underline rule)."""
        styled = parse_markup("<s>foo bar</s>")
        words, separators = split_styled_words_with_separators(styled)
        assert [word.text for word in words] == ["foo", "bar"]
        # The space between the words is inside the strike span.
        assert separators[0] == STRIKE


class TestStrikethroughRegistry:
    """The <s> registry entry."""

    def test_strikethrough_is_registered(self) -> None:
        """The shipped tag is present, idempotent, and takes no args."""
        spec = TAG_REGISTRY["s"]
        assert spec.apply(PLAIN, "") == STRIKE
        assert spec.apply(STRIKE, "") == STRIKE
        # Applying <s> inside <i> composes (idempotent per field).
        assert spec.apply(ITALIC, "") == Style(italic=True, strikethrough=True)
        assert spec.accepts_args is False

    def test_nested_strikethrough_is_idempotent(self) -> None:
        """<s> inside <s> has no additional effect (the TODO's contract)."""
        styled = parse_markup("<s>a<s>b</s>c</s>")
        assert styled.plain_text == "abc"
        assert styled.strikethrough_flags == (True, True, True)
        # The whole line is one struck run (adjacent same-style runs merge).
        assert len(styled.runs) == 1

    def test_strike_and_underline_are_distinct_tags(self) -> None:
        """Registry order (i, u, b, scale, sup, sub, s, sc, f, c, oc, vc, tc, cb) fixes nesting."""
        assert list(TAG_REGISTRY) == [
            "i",
            "u",
            "b",
            "scale",
            "sup",
            "sub",
            "s",
            "sc",
            "f",
            "c",
            "oc",
            "vc",
            "tc",
            "cb",
        ]

    def test_nested_strike_serializes_flat(self) -> None:
        """The idempotent nesting serializes to one span (no nested <s>)."""
        styled = parse_markup("<s>a<s>b</s>c</s>")
        assert serialize_runs(styled.runs) == "<s>abc</s>"


SMALL_CAPS = Style(small_caps=True)


class TestParseSmallCaps:
    """The <sc> tag: inline small caps over the plain text."""

    def test_small_caps_run_splits_the_line(self) -> None:
        """plain <sc>X</sc> plain yields plain/small-caps/plain runs."""
        styled = parse_markup("plain <sc>X</sc> plain")
        assert styled.plain_text == "plain X plain"
        assert styled.has_markup is True
        flags = styled.small_caps_flags
        assert len(flags) == 13
        assert flags[6] is True
        assert sum(flags) == 1

    def test_argumented_form_is_literal_text(self) -> None:
        """<sc> takes no args, so <sc:0.5> engraves verbatim (the <b:2> rule)."""
        styled = parse_markup("<sc:0.5>text</sc:0.5>")
        assert styled.has_markup is False
        assert styled.plain_text == "<sc:0.5>text</sc:0.5>"

    def test_unclosed_small_caps_raises(self) -> None:
        """An unclosed <sc> is a hard error (the fail-loud contract)."""
        with pytest.raises(MarkupError, match="Unclosed tag <sc>"):
            parse_markup("<sc>abc")

    def test_mismatched_close_raises(self) -> None:
        """</sc> with no open <sc> is a hard error (never opened)."""
        with pytest.raises(MarkupError, match="never opened"):
            parse_markup("<i>x</sc>")

    def test_small_caps_inside_italic_composes(self) -> None:
        """<sc> inside <i> carries both flags."""
        styled = parse_markup("<i>nested <sc>markup</sc> tail</i>")
        assert styled.plain_text == "nested markup tail"
        styles = [style for run in styled.runs for style in (run.style,) * len(run.text)]
        assert styles[7] == Style(italic=True, small_caps=True)
        assert styles[0] == ITALIC

    def test_serialization_round_trips(self) -> None:
        """The small-caps tag re-emits and re-parses identically."""
        raw = "<sc>caps</sc> and <i><sc>both</sc></i>"
        styled = parse_markup(raw)
        assert serialize_runs(styled.runs) == raw
        assert parse_markup(raw).runs == styled.runs

    def test_nested_word_serialization_round_trips(self) -> None:
        """A small-caps word token round-trips (the reflow contract)."""
        styled = parse_markup("x <sc>abc</sc> y")
        words = split_styled_words(styled)
        assert [word.text for word in words] == ["x", "abc", "y"]
        assert serialize_word(words[1]) == "<sc>abc</sc>"
        reparsed = parse_markup(serialize_word(words[1]))
        assert reparsed.plain_text == "abc"
        assert all(reparsed.small_caps_flags)

    def test_small_caps_span_covering_a_space_keeps_the_separator(self) -> None:
        """<sc>foo bar</sc> tokenizes with the small-caps separator (underline rule)."""
        styled = parse_markup("<sc>foo bar</sc>")
        words, separators = split_styled_words_with_separators(styled)
        assert [word.text for word in words] == ["foo", "bar"]
        # The space between the words is inside the small-caps span.
        assert separators[0] == SMALL_CAPS


class TestSmallCapsRegistry:
    """The <sc> registry entry."""

    def test_small_caps_is_registered(self) -> None:
        """The shipped tag is present, idempotent, and takes no args."""
        spec = TAG_REGISTRY["sc"]
        assert spec.apply(PLAIN, "") == SMALL_CAPS
        assert spec.apply(SMALL_CAPS, "") == SMALL_CAPS
        # Applying <sc> inside <i> composes (idempotent per field).
        assert spec.apply(ITALIC, "") == Style(italic=True, small_caps=True)
        assert spec.accepts_args is False
        assert spec.args_optional is False

    def test_nested_small_caps_is_idempotent(self) -> None:
        """<sc> inside <sc> has no additional effect (the TODO's contract)."""
        styled = parse_markup("<sc>a<sc>b</sc>c</sc>")
        assert styled.plain_text == "abc"
        assert styled.small_caps_flags == (True, True, True)
        # The whole line is one small-caps run (adjacent same-style runs merge).
        assert len(styled.runs) == 1

    def test_nested_small_caps_serializes_flat(self) -> None:
        """The idempotent nesting serializes to one span (no nested <sc>)."""
        styled = parse_markup("<sc>a<sc>b</sc>c</sc>")
        assert serialize_runs(styled.runs) == "<sc>abc</sc>"

    def test_small_caps_is_registered_last(self) -> None:
        """Registry order (i, u, b, scale, sup, sub, s, sc, f, c, oc, vc, tc, cb) fixes nesting."""
        assert list(TAG_REGISTRY) == [
            "i",
            "u",
            "b",
            "scale",
            "sup",
            "sub",
            "s",
            "sc",
            "f",
            "c",
            "oc",
            "vc",
            "tc",
            "cb",
        ]

    def test_small_caps_and_scale_are_distinct_tags(self) -> None:
        """<sc> (small caps) and <scale:N> (silhouette scale) are separate fields."""
        styled = parse_markup("<scale:0.5><sc>both</sc></scale>")
        assert styled.plain_text == "both"
        assert styled.scale_values == (0.5, 0.5, 0.5, 0.5)
        assert styled.small_caps_flags == (True, True, True, True)

    def test_style_boundaries_see_the_small_caps_run(self) -> None:
        """A small-caps span is a style boundary for the reflow's scope guard."""
        styled = parse_markup("ab<sc>cd</sc>ef")
        assert style_boundaries(styled) == (2, 4)


FONTS = Style(font="Wingdings")


def _upper_resolver(name: str) -> str:
    """A test font resolver: canonicalize by upper-casing, reject 'bad'."""
    if name.lower() == "bad":
        raise ValueError(f"Unknown font {name!r}.")
    return name.upper()


class TestFontRegistry:
    """The <f> registry entry (the font-selection value tag)."""

    def test_font_is_registered_with_args(self) -> None:
        """The tag accepts a mandatory arg (the <c:D> tag registers after it)."""
        spec = TAG_REGISTRY["f"]
        assert spec.accepts_args is True
        assert spec.args_optional is False
        assert spec.apply(PLAIN, "Wingdings") == FONTS
        # Set-to-value: re-applying with the same argument is idempotent.
        assert spec.apply(FONTS, "Wingdings") == FONTS
        # A new argument replaces the active font.
        assert spec.apply(FONTS, "Dino") == Style(font="Dino")
        # Applying <f> inside <i> composes.
        assert spec.apply(ITALIC, "Wingdings") == Style(italic=True, font="Wingdings")

    def test_is_active_predicate(self) -> None:
        """The tag is active exactly when a font is selected."""
        spec = TAG_REGISTRY["f"]
        assert spec.is_active(PLAIN) is False
        assert spec.is_active(FONTS) is True

    def test_arg_serializer_round_trip(self) -> None:
        """The arg serializer emits the canonical argument the parser accepts."""
        spec = TAG_REGISTRY["f"]
        assert spec.arg is not None
        assert spec.arg(FONTS) == "Wingdings"
        assert spec.apply(PLAIN, spec.arg(FONTS)) == FONTS

    def test_arg_serializer_reencodes_spaces(self) -> None:
        r"""A font name with a space re-encodes as '+' (the \S* argument rule)."""
        spec = TAG_REGISTRY["f"]
        assert spec.arg(Style(font="Foo Bar")) == "Foo+Bar"
        assert spec.apply(PLAIN, "Foo+Bar") == Style(font="Foo Bar")

    def test_plus_decodes_to_space(self) -> None:
        """`+` is the canonical space of the argument (decoded on parse)."""
        styled = parse_markup("<f:Foo+Bar>x</f>")
        assert styled.font_values == ("Foo Bar",)

    def test_underscore_stays_literal(self) -> None:
        """Catalog names keep underscores (ReliefSingleLineCAD-Regular style)."""
        styled = parse_markup("<f:Foo_Bar>x</f>")
        assert styled.font_values == ("Foo_Bar",)


class TestFontParse:
    """Parsing the <f:font name> tag."""

    def test_font_run_and_values(self) -> None:
        """The run carries the font and font_values align with plain_text."""
        styled = parse_markup("Hello <f:Wingdings>abcd</f> world")
        assert styled.plain_text == "Hello abcd world"
        assert styled.has_markup is True
        assert styled.runs == (
            Run("Hello ", PLAIN),
            Run("abcd", FONTS),
            Run(" world", PLAIN),
        )
        assert styled.font_values == (None,) * 6 + ("Wingdings",) * 4 + (None,) * 6

    def test_bare_f_tag_is_literal(self) -> None:
        """A font name is mandatory: <f> without an argument engraves literally."""
        styled = parse_markup("a <f>x")
        assert styled.plain_text == "a <f>x"
        assert styled.has_markup is False

    def test_empty_argument_is_a_hard_error(self) -> None:
        """<f:> aborts loudly (MarkupError, fail-loud contract)."""
        with pytest.raises(MarkupError, match="font tag argument is empty"):
            parse_markup("<f:>x</f>")

    def test_nested_font_restores_the_outer(self) -> None:
        """The close of an inner font tag restores the outer font."""
        styled = parse_markup("<f:Stylus>A<f:Wingdings>B</f>C</f>D")
        assert styled.font_values == ("Stylus", "Wingdings", "Stylus", None)

    def test_font_inside_italic_composes(self) -> None:
        """A font span nested in italic carries both styles."""
        styled = parse_markup("<i>it <f:Wingdings>mix</f></i>")
        assert styled.italic_flags == (True, True, True, True, True, True)
        assert styled.font_values == (None, None, None, "Wingdings", "Wingdings", "Wingdings")

    def test_crossing_font_spans_are_legal(self) -> None:
        """A crossing font close replays the remaining opens (the crossing rule)."""
        styled = parse_markup("<f:A>ab<f:B>cd</f>ef</f>")
        # 'ab' A, 'cd' B, 'ef' A (the close of the inner <f> restores A).
        assert styled.font_values == ("A", "A", "B", "B", "A", "A")


class TestFontResolver:
    """The injected font_resolver canonicalizes and validates the argument."""

    def test_resolver_canonicalizes_at_open(self) -> None:
        """The resolver's canonical name lands in the style (and the stack)."""
        styled = parse_markup("<f:dino>x</f>", font_resolver=_upper_resolver)
        assert styled.font_values == ("DINO",)

    def test_resolver_replay_reuses_the_canonical_name(self) -> None:
        """A crossing close replays the canonical argument (no re-resolving)."""
        styled = parse_markup("<f:dino>a<f:wingdings>b</f>c</f>", font_resolver=_upper_resolver)
        assert styled.font_values == ("DINO", "WINGDINGS", "DINO")

    def test_unknown_font_is_a_hard_error(self) -> None:
        """A resolver's ValueError becomes MarkupError (fail-loud contract)."""
        with pytest.raises(MarkupError, match="Unknown font"):
            parse_markup("<f:bad>x</f>", font_resolver=_upper_resolver)

    def test_no_resolver_keeps_the_decoded_argument(self) -> None:
        """Direct API callers (no resolver) keep the raw decoded name."""
        styled = parse_markup("<f:dino>x</f>")
        assert styled.font_values == ("dino",)

    def test_resolver_runs_on_every_open(self) -> None:
        """Each open resolves its own argument (per-run fonts)."""
        styled = parse_markup("<f:dino>a<f:wing>b</f></f>", font_resolver=_upper_resolver)
        assert styled.font_values == ("DINO", "WING")


class TestFontSerialize:
    """The serialize round-trip keeps the font argument (the reflow depends on it)."""

    @pytest.mark.parametrize(
        "raw",
        [
            "Hello <f:Wingdings>abcd</f> world",
            "<f:Stylus>A<f:Wingdings>B</f>C</f>D",
            "<i>it </i><i><f:Wingdings>mix</f></i>",
            "<b>bd <f:Foo+Bar>sym</f></b>",
        ],
    )
    def test_round_trip_is_lossless(self, raw: str) -> None:
        """Serialization re-emits the argument and re-parses to the same runs."""
        styled = parse_markup(raw)
        serialized = serialize_runs(styled.runs)
        assert parse_markup(serialized).runs == styled.runs

    def test_serialize_word_keeps_the_font_argument(self) -> None:
        """A styled word token round-trips with its font."""
        styled = parse_markup("big <f:Wingdings>sym</f>")
        words = split_styled_words(styled)
        assert [word.text for word in words] == ["big", "sym"]
        assert serialize_word(words[1]) == "<f:Wingdings>sym</f>"
        assert serialize_word(words[0]) == "big"

    def test_style_boundaries_see_the_font_change(self) -> None:
        """A font span's edges are style boundaries (the reflow's guard)."""
        styled = parse_markup("ab<f:Wingdings>cd</f>ef")
        assert style_boundaries(styled) == (2, 4)

    def test_space_spanning_font_run_keeps_the_separator(self) -> None:
        """<f:X>foo bar</f> tokenizes with the font separator (underline rule)."""
        styled = parse_markup("<f:Wingdings>foo bar</f>")
        words, separators = split_styled_words_with_separators(styled)
        assert [word.text for word in words] == ["foo", "bar"]
        assert all(sep.font == "Wingdings" for sep in separators)


CUTTER = Style(cutter=0.06)


class TestCutterRegistry:
    """The <c:D> registry entry (the explicit-cutter value tag)."""

    def test_cutter_is_registered_with_args(self) -> None:
        """The tag accepts a mandatory arg and is registered after every other value tag."""
        spec = TAG_REGISTRY["c"]
        assert spec.accepts_args is True
        assert spec.args_optional is False
        # The <oc> flag tag was registered after <c>, so <c> is no longer last.
        assert list(TAG_REGISTRY).index("c") == list(TAG_REGISTRY).index("f") + 1
        assert list(TAG_REGISTRY)[-1] == "cb"
        assert spec.apply(PLAIN, "0.06") == CUTTER
        # Set-to-value: re-applying with the same argument is idempotent.
        assert spec.apply(CUTTER, "0.06") == CUTTER
        # A new argument replaces the active cutter.
        assert spec.apply(CUTTER, "0.09") == Style(cutter=0.09)
        # Applying <c> inside <i> composes.
        assert spec.apply(ITALIC, "0.06") == Style(italic=True, cutter=0.06)

    def test_is_active_predicate(self) -> None:
        """The tag is active exactly when a cutter is selected."""
        spec = TAG_REGISTRY["c"]
        assert spec.is_active(PLAIN) is False
        assert spec.is_active(CUTTER) is True

    def test_arg_serializer_round_trip(self) -> None:
        """The arg serializer emits the canonical argument the parser accepts."""
        spec = TAG_REGISTRY["c"]
        assert spec.arg is not None
        assert spec.arg(CUTTER) == "0.06"
        assert spec.apply(PLAIN, spec.arg(CUTTER)) == CUTTER

    def test_registry_order_is_stable(self) -> None:
        """The tag is registered after every shipped tag (the serialization order)."""
        assert list(TAG_REGISTRY) == [
            "i",
            "u",
            "b",
            "scale",
            "sup",
            "sub",
            "s",
            "sc",
            "f",
            "c",
            "oc",
            "vc",
            "tc",
            "cb",
        ]


class TestCutterParse:
    """Parsing the <c:D> tag (the explicit cutter diameter)."""

    def test_cutter_run_and_values(self) -> None:
        """The run carries the diameter and the per-char values align."""
        styled = parse_markup("A<c:0.06>B</c>C")
        assert styled.plain_text == "ABC"
        assert styled.cutter_values == (None, 0.06, None)

    def test_bare_cutter_is_literal_text(self) -> None:
        """The diameter is mandatory, so a bare <c> is not markup (the <scale> rule).

        A bare open tag is literal text; the matching close then has no open
        to close, which is the framework's hard error for a mandatory-arg tag
        (exactly the ``<scale>`` / ``<f>`` behavior).
        """
        styled = parse_markup("a <c> b")
        assert styled.plain_text == "a <c> b"
        assert styled.has_markup is False
        with pytest.raises(MarkupError, match="Unexpected closing tag"):
            parse_markup("<c>text</c>")

    def test_non_numeric_argument_is_an_error(self) -> None:
        """A non-numeric diameter is broken markup (fail-loud)."""
        with pytest.raises(MarkupError) as excinfo:
            parse_markup("<c:abc>x</c>")
        assert "not a number" in str(excinfo.value)

    def test_empty_argument_is_an_error(self) -> None:
        """An empty diameter is broken markup (fail-loud)."""
        with pytest.raises(MarkupError) as excinfo:
            parse_markup("<c:>x</c>")
        assert "not a number" in str(excinfo.value)

    def test_non_positive_argument_is_an_error(self) -> None:
        """A zero or negative diameter is out of the supported range."""
        for bad in ("0", "-0.06"):
            with pytest.raises(MarkupError) as excinfo:
                parse_markup(f"<c:{bad}>x</c>")
            assert "outside the supported range" in str(excinfo.value)

    def test_oversized_argument_is_an_error(self) -> None:
        """A diameter above the ceiling is out of the supported range."""
        with pytest.raises(MarkupError) as excinfo:
            parse_markup("<c:1.5>x</c>")
        assert "outside the supported range" in str(excinfo.value)

    def test_the_cutter_is_independent_of_the_other_tags(self) -> None:
        """<c:D> is its own field (never confused with <scale:N> or <f:>)."""
        styled = parse_markup("<c:0.06><scale:0.5>x</scale></c>")
        assert styled.cutter_values == (0.06,)
        assert styled.scale_values == (0.5,)
        assert styled.font_values == (None,)


class TestCutterSerialize:
    """The <c:D> serialization round-trip (the reflow depends on it)."""

    def test_serialized_form_reparses_identically(self) -> None:
        """serialize_runs -> parse_markup reproduces the same per-char values."""
        styled = parse_markup("A<c:0.06>B</c>C")
        raw = serialize_runs(styled.runs)
        assert raw == "A<c:0.06>B</c>C"
        assert parse_markup(raw).cutter_values == styled.cutter_values

    def test_nested_cutter_restores_the_outer_value(self) -> None:
        """A nested <c:D> restores the outer diameter for the trailing text."""
        styled = parse_markup("<c:0.06>a<c:0.09>b</c>c</c>")
        assert styled.cutter_values == (0.06, 0.09, 0.06)
        raw = serialize_runs(styled.runs)
        assert parse_markup(raw).cutter_values == styled.cutter_values

    def test_style_boundaries_see_the_cutter_change(self) -> None:
        """A cutter span's edges are style boundaries (the reflow's guard)."""
        styled = parse_markup("ab<c:0.06>cd</c>ef")
        assert style_boundaries(styled) == (2, 4)


class TestCutterWords:
    """The <c:D> reflow tokens (the line-content reflow round-trip)."""

    def test_styled_word_keeps_the_cutter_argument(self) -> None:
        """A styled word token round-trips with its cutter."""
        styled = parse_markup("big <c:0.06>cut</c>")
        words = split_styled_words(styled)
        assert [word.text for word in words] == ["big", "cut"]
        assert serialize_word(words[1]) == "<c:0.06>cut</c>"
        assert serialize_word(words[0]) == "big"

    def test_space_spanning_cutter_run_keeps_the_separator(self) -> None:
        """<c:X>foo bar</c> tokenizes with the cutter separator (underline rule)."""
        styled = parse_markup("<c:0.06>foo bar</c>")
        words, separators = split_styled_words_with_separators(styled)
        assert [word.text for word in words] == ["foo", "bar"]
        assert all(sep.cutter == 0.06 for sep in separators)

class TestOriginalCutterRegistry:
    """The <oc> registry entry (the original-cutter flag tag)."""

    def test_original_cutter_is_registered_as_a_flag(self) -> None:
        """The tag takes no args and sits after every value tag (order pin)."""
        spec = TAG_REGISTRY["oc"]
        assert spec.accepts_args is False
        assert spec.args_optional is False
        assert spec.arg is None
        assert list(TAG_REGISTRY)[-4] == "oc"
        assert list(TAG_REGISTRY).index("vc") == list(TAG_REGISTRY).index("oc") + 1
        assert spec.apply(PLAIN, "") == Style(original_cutter=True)
        # Set-to-True: re-applying is idempotent (nesting <oc> in <oc> is a no-op).
        assert spec.apply(Style(original_cutter=True), "") == Style(original_cutter=True)

    def test_is_active_predicate(self) -> None:
        """The tag is active exactly when the segment is pinned."""
        spec = TAG_REGISTRY["oc"]
        assert spec.is_active(PLAIN) is False
        assert spec.is_active(Style(original_cutter=True)) is True

    def test_original_cutter_composes_with_the_other_tags(self) -> None:
        """Applying <oc> inside <i> composes (idempotent per field)."""
        spec = TAG_REGISTRY["oc"]
        assert spec.apply(ITALIC, "") == Style(italic=True, original_cutter=True)
        assert spec.apply(BOLD, "") == Style(bold=True, original_cutter=True)


class TestOriginalCutterParse:
    """Parsing the <oc> tag (the original-cutter pin)."""

    def test_original_cutter_flags_align_with_the_plain_text(self) -> None:
        """The run carries the pin and the per-char flags align."""
        styled = parse_markup("A<oc>B</oc>C")
        assert styled.plain_text == "ABC"
        assert styled.original_cutter_flags == (False, True, False)

    def test_a_bare_open_tag_is_markup(self) -> None:
        """The tag carries no argument, so the bare <oc> IS markup."""
        styled = parse_markup("<oc>x</oc>")
        assert styled.plain_text == "x"
        assert styled.original_cutter_flags == (True,)

    def test_an_argument_form_is_literal_text(self) -> None:
        """<oc:0.06> is not a legal form (the tag takes no argument)."""
        styled = parse_markup("a <oc:0.06> b")
        assert styled.plain_text == "a <oc:0.06> b"
        assert styled.has_markup is False

    def test_an_unclosed_tag_is_a_hard_error(self) -> None:
        """An opener never closed raises MarkupError (the fail-loud contract)."""
        with pytest.raises(MarkupError, match="Unclosed tag"):
            parse_markup("<oc>text")

    def test_the_pin_composes_with_a_scale_run(self) -> None:
        """The motivating case: bigger text, the line's own tool."""
        styled = parse_markup("<oc><scale:2.0>x</scale></oc>")
        assert styled.original_cutter_flags == (True,)
        assert styled.scale_values == (2.0,)

    def test_the_pin_is_independent_of_the_cutter_tag(self) -> None:
        """<oc> and <c:D> are separate fields (they compose, never confuse)."""
        styled = parse_markup("<oc><c:0.06>x</c></oc>")
        assert styled.original_cutter_flags == (True,)
        assert styled.cutter_values == (0.06,)


class TestOriginalCutterSerialize:
    """The <oc> serialization round-trip (the reflow depends on it)."""

    def test_the_tag_round_trips(self) -> None:
        """serialize_runs re-emits <oc>/</oc> and re-parses to the same runs."""
        styled = parse_markup("A<oc>B</oc>C")
        raw = serialize_runs(styled.runs)
        assert raw == "A<oc>B</oc>C"
        assert parse_markup(raw).runs == styled.runs

    def test_a_nested_pin_restores_the_outer_style(self) -> None:
        """A nested <oc> restores the outer run for the trailing text."""
        styled = parse_markup("<oc>a<oc>b</oc>c</oc>")
        assert styled.original_cutter_flags == (True, True, True)
        raw = serialize_runs(styled.runs)
        assert parse_markup(raw).original_cutter_flags == styled.original_cutter_flags

    def test_the_pin_survives_a_scale_composition(self) -> None:
        """The <oc> pin nests inside the <scale:N> tag and round-trips."""
        styled = parse_markup("<oc><scale:2.0>x</scale></oc>")
        raw = serialize_runs(styled.runs)
        assert "<oc>" in raw
        assert parse_markup(raw).runs == styled.runs


class TestOriginalCutterWords:
    """The <oc> reflow tokens (the line-content reflow round-trip)."""

    def test_styled_word_keeps_the_pin(self) -> None:
        """A styled word token round-trips with the pin."""
        styled = parse_markup("plain <oc>pinned</oc>")
        words = split_styled_words(styled)
        assert [word.text for word in words] == ["plain", "pinned"]
        assert serialize_word(words[1]) == "<oc>pinned</oc>"
        assert serialize_word(words[0]) == "plain"

    def test_space_spanning_pin_run_keeps_the_separator(self) -> None:
        """<oc>foo bar</oc> tokenizes with the pinned separator."""
        styled = parse_markup("<oc>foo bar</oc>")
        words, separators = split_styled_words_with_separators(styled)
        assert [word.text for word in words] == ["foo", "bar"]
        assert all(sep.original_cutter for sep in separators)


class TestVCenterRegistry:
    """The <vc> registry entry (the vertical-centering flag tag)."""

    def test_v_center_is_registered_as_a_flag(self) -> None:
        """The tag takes no args and is registered after <oc> (before <tc> and <cb>)."""
        spec = TAG_REGISTRY["vc"]
        assert spec.accepts_args is False
        assert spec.args_optional is False
        assert spec.arg is None
        assert list(TAG_REGISTRY)[-1] == "cb"
        assert list(TAG_REGISTRY).index("vc") == list(TAG_REGISTRY).index("oc") + 1
        assert spec.apply(PLAIN, "") == Style(v_center=True)
        # Set-to-True: re-applying is idempotent (nesting <vc> in <vc> is a no-op).
        assert spec.apply(Style(v_center=True), "") == Style(v_center=True)

    def test_is_active_predicate(self) -> None:
        """The tag is active exactly when the segment is centred."""
        spec = TAG_REGISTRY["vc"]
        assert spec.is_active(PLAIN) is False
        assert spec.is_active(Style(v_center=True)) is True

    def test_v_center_composes_with_the_other_tags(self) -> None:
        """Applying <vc> inside <i>/<b> composes (idempotent per field)."""
        spec = TAG_REGISTRY["vc"]
        assert spec.apply(ITALIC, "") == Style(italic=True, v_center=True)
        assert spec.apply(BOLD, "") == Style(bold=True, v_center=True)


class TestVCenterParse:
    """Parsing the <vc> tag (the vertical centering)."""

    def test_v_center_flags_align_with_the_plain_text(self) -> None:
        """The run carries the centring and the per-char flags align."""
        styled = parse_markup("A<vc>-</vc>C")
        assert styled.plain_text == "A-C"
        assert styled.v_center_flags == (False, True, False)

    def test_a_bare_open_tag_is_markup(self) -> None:
        """The tag carries no argument, so the bare <vc> IS markup."""
        styled = parse_markup("<vc>x</vc>")
        assert styled.plain_text == "x"
        assert styled.v_center_flags == (True,)

    def test_an_argument_form_is_literal_text(self) -> None:
        """<vc:0.5> is not a legal form (the tag takes no argument)."""
        styled = parse_markup("a <vc:0.5> b")
        assert styled.plain_text == "a <vc:0.5> b"
        assert styled.has_markup is False

    def test_an_unclosed_tag_is_a_hard_error(self) -> None:
        """An opener never closed raises MarkupError (the fail-loud contract)."""
        with pytest.raises(MarkupError, match="Unclosed tag"):
            parse_markup("<vc>text")

    def test_the_centring_composes_with_a_scale_run(self) -> None:
        """<vc> and <scale:N> are separate fields (they compose)."""
        styled = parse_markup("<vc><scale:2.0>x</scale></vc>")
        assert styled.v_center_flags == (True,)
        assert styled.scale_values == (2.0,)

    def test_crossing_spans_intersect_like_every_tag(self) -> None:
        """<vc> crossing <b> follows the per-character intersection rule."""
        styled = parse_markup("<b>ab<vc>c</b>d</vc>")
        assert styled.plain_text == "abcd"
        assert styled.bold_flags == (True, True, True, False)
        assert styled.v_center_flags == (False, False, True, True)


class TestVCenterSerialize:
    """The <vc> serialization round-trip (the reflow depends on it)."""

    def test_the_tag_round_trips(self) -> None:
        """serialize_runs re-emits <vc>/</vc> and re-parses to the same runs."""
        styled = parse_markup("A<vc>B</vc>C")
        raw = serialize_runs(styled.runs)
        assert raw == "A<vc>B</vc>C"
        assert parse_markup(raw).runs == styled.runs

    def test_a_nested_centre_restores_the_outer_style(self) -> None:
        """A nested <vc> restores the outer run for the trailing text."""
        styled = parse_markup("<vc>a<vc>b</vc>c</vc>")
        assert styled.v_center_flags == (True, True, True)
        raw = serialize_runs(styled.runs)
        assert parse_markup(raw).v_center_flags == styled.v_center_flags

    def test_the_centre_survives_a_scale_composition(self) -> None:
        """The <vc> tag nests inside the <scale:N> tag and round-trips."""
        styled = parse_markup("<vc><scale:2.0>x</scale></vc>")
        raw = serialize_runs(styled.runs)
        assert "<vc>" in raw
        assert parse_markup(raw).runs == styled.runs


class TestVCenterWords:
    """The <vc> reflow tokens (the line-content reflow round-trip)."""

    def test_styled_word_keeps_the_centring(self) -> None:
        """A styled word token round-trips with the centring."""
        styled = parse_markup("plain <vc>centred</vc>")
        words = split_styled_words(styled)
        assert [word.text for word in words] == ["plain", "centred"]
        assert serialize_word(words[1]) == "<vc>centred</vc>"
        assert serialize_word(words[0]) == "plain"

    def test_space_spanning_centre_run_keeps_the_separator(self) -> None:
        """<vc>foo bar</vc> tokenizes with the centred separator."""
        styled = parse_markup("<vc>foo bar</vc>")
        words, separators = split_styled_words_with_separators(styled)
        assert [word.text for word in words] == ["foo", "bar"]
        assert all(sep.v_center for sep in separators)


class TestKernMarkers:
    """The manual-kern marker strings (kern_pos_string / kern_neg_string)."""

    def test_no_markers_keeps_the_historical_shape(self) -> None:
        """A line parsed without marker strings is untouched."""
        styled = parse_markup("some text")
        assert styled.plain_text == "some text"
        assert styled.kern_values == (0,) * 9
        assert not styled.has_markup

    def test_markers_disabled_by_default(self) -> None:
        """Without the kern kwargs, a marker-shaped string stays literal text."""
        styled = parse_markup("some tex<->t")
        assert styled.plain_text == "some tex<->t"
        assert styled.kern_values == (0,) * 12

    def test_negative_marker_stripped_and_counted(self) -> None:
        """The marker vanishes from plain_text and counts -1 on the next char."""
        styled = parse_markup("some tex<->t", kern_pos="<+>", kern_neg="<->")
        assert styled.plain_text == "some text"
        assert styled.kern_values == (0, 0, 0, 0, 0, 0, 0, 0, -1)
        assert styled.has_markup  # a stripped marker routes the exact path

    def test_positive_marker_counts_plus_one(self) -> None:
        """The positive marker counts +1 on the following character."""
        styled = parse_markup("A<+>B", kern_pos="<+>", kern_neg="<->")
        assert styled.plain_text == "AB"
        assert styled.kern_values == (0, 1)

    def test_adjacent_markers_accumulate(self) -> None:
        """Consecutive markers stack on the same character (+2 / -3)."""
        styled = parse_markup("A<+><+>B<-><-><->C", kern_pos="<+>", kern_neg="<->")
        assert styled.plain_text == "ABC"
        assert styled.kern_values == (0, 2, -3)

    def test_mixed_markers_net_out(self) -> None:
        """A positive and a negative marker on one character net to 0."""
        styled = parse_markup("A<+><->B", kern_pos="<+>", kern_neg="<->")
        assert styled.plain_text == "AB"
        assert styled.kern_values == (0, 0)

    def test_leading_marker_shifts_the_first_glyph(self) -> None:
        """A marker at position 0 attaches to the first character."""
        styled = parse_markup("<+>ABC", kern_pos="<+>", kern_neg="<->")
        assert styled.plain_text == "ABC"
        assert styled.kern_values == (1, 0, 0)

    def test_trailing_marker_is_dropped(self) -> None:
        """A marker with no following character is a no-op (documented)."""
        styled = parse_markup("ABC<->", kern_pos="<+>", kern_neg="<->")
        assert styled.plain_text == "ABC"
        assert styled.kern_values == (0, 0, 0)

    def test_marker_before_a_space_attaches_to_the_next_word(self) -> None:
        """A space owns no glyph: the count rides the next non-space char."""
        styled = parse_markup("foo<+> bar", kern_pos="<+>", kern_neg="<->")
        assert styled.plain_text == "foo bar"
        # f o o ' ' b a r -- the +1 lands on 'b' (index 4), not the space.
        assert styled.kern_values == (0, 0, 0, 0, 1, 0, 0)

    def test_marker_inside_a_tag_span(self) -> None:
        """A marker inside an <i> span strips and keeps the span's style."""
        styled = parse_markup("<i>foo<->bar</i>", kern_pos="<+>", kern_neg="<->")
        assert styled.plain_text == "foobar"
        assert styled.italic_flags == (True,) * 6
        assert styled.kern_values == (0, 0, 0, -1, 0, 0)

    def test_tag_scan_wins_over_the_marker(self) -> None:
        """A marker that is a registered tag is consumed as markup first."""
        # <s> opens the strikethrough tag: an unclosed tag is a hard error,
        # so the marker never strips.
        with pytest.raises(MarkupError):
            parse_markup("a<s>b", kern_pos="<s>", kern_neg="<->")
        closed = parse_markup("a<s>b</s>", kern_pos="<s>", kern_neg="<->")
        assert closed.strikethrough_flags == (False, True)
        assert closed.kern_values == (0, 0)  # never a marker: it is markup

    def test_multi_char_non_bracket_marker(self) -> None:
        """A marker without brackets (e.g. '~~') strips like a bracketed one."""
        styled = parse_markup("A~~B", kern_pos="~~", kern_neg=None)
        assert styled.plain_text == "AB"
        assert styled.kern_values == (0, 1)

    def test_positive_wins_a_shared_prefix(self) -> None:
        """The documented check order: the positive marker claims a shared prefix."""
        # pos '<+' is a prefix of neg '<+->': the positive matches first and
        # the leftover '- >' engraves literally.
        styled = parse_markup("A<+->B", kern_pos="<+", kern_neg="<+->")
        assert styled.plain_text == "A->B"
        assert styled.kern_values == (0, 1, 0, 0)

    def test_serialize_round_trips_markers(self) -> None:
        """serialize_runs re-emits the markers; re-parsing yields identical runs."""
        styled = parse_markup("some tex<->t", kern_pos="<+>", kern_neg="<->")
        raw = serialize_runs(styled.runs, "<", ">", "<+>", "<->")
        assert raw == "some tex<->t"
        assert parse_markup(raw, kern_pos="<+>", kern_neg="<->").runs == styled.runs

    def test_serialize_round_trips_inside_tag_spans(self) -> None:
        """A marker inside a tag span re-emits inside the tags and round-trips."""
        styled = parse_markup("<i>foo<->bar</i>", kern_pos="<+>", kern_neg="<->")
        raw = serialize_runs(styled.runs, "<", ">", "<+>", "<->")
        assert parse_markup(raw, kern_pos="<+>", kern_neg="<->").runs == styled.runs

    def test_serialize_without_strings_drops_the_counts(self) -> None:
        """Direct-API serialization without the strings drops markers (documented)."""
        styled = parse_markup("A<+>B", kern_pos="<+>", kern_neg="<->")
        assert serialize_runs(styled.runs) == "AB"

    def test_serialize_word_round_trips_markers(self) -> None:
        """A styled word token keeps its markers through serialize_word."""
        styled = parse_markup("foo A<+>B bar", kern_pos="<+>", kern_neg="<->")
        words = split_styled_words(styled)
        assert [word.text for word in words] == ["foo", "AB", "bar"]
        assert serialize_word(words[1], "<", ">", "<+>", "<->") == "A<+>B"
        reparsed = parse_markup(serialize_word(words[1], "<", ">", "<+>", "<->"), kern_pos="<+>", kern_neg="<->")
        assert reparsed.kern_values == (0, 1)

    def test_custom_brackets_marker(self) -> None:
        """A marker legal under the line's custom brackets strips correctly."""
        # «» brackets: <s> is literal text under them and acts as a marker.
        styled = parse_markup("a<s>b", "\u00ab", "\u00bb", kern_pos="<s>", kern_neg="</s>")
        assert styled.plain_text == "ab"
        assert styled.kern_values == (0, 1)

    def test_blank_line_with_marker(self) -> None:
        """A line of only markers yields an empty plain text."""
        styled = parse_markup("<+><->", kern_pos="<+>", kern_neg="<->")
        assert styled.plain_text == ""
        assert styled.kern_values == ()


class TestTextColorRegistry:
    """The <tc:color> registry entry (the stroke-color value tag)."""

    def test_text_color_is_registered_with_args(self) -> None:
        """The tag accepts a mandatory arg and is registered second-to-last (before <cb>)."""
        spec = TAG_REGISTRY["tc"]
        assert spec.accepts_args is True
        assert spec.args_optional is False
        assert spec.arg is not None
        assert list(TAG_REGISTRY).index("tc") == list(TAG_REGISTRY).index("vc") + 1
        assert list(TAG_REGISTRY)[-1] == "cb"

    def test_apply_is_set_to_value_and_idempotent(self) -> None:
        """Applying the same color twice is the identity (the <f:...> rule)."""
        spec = TAG_REGISTRY["tc"]
        red = Style(text_color="red")
        assert spec.apply(PLAIN, "red") == red
        assert spec.apply(red, "red") == red
        # A new color replaces the active one (nested <tc:b> restores the
        # outer for the trailing text via the close's pre-open pop).
        assert spec.apply(red, "blue") == Style(text_color="blue")

    def test_is_active_predicate(self) -> None:
        """The tag is active exactly on styles carrying a color."""
        spec = TAG_REGISTRY["tc"]
        assert spec.is_active(PLAIN) is False
        assert spec.is_active(Style(text_color="red")) is True

    def test_empty_argument_is_an_error(self) -> None:
        """An empty color is broken markup (fail-loud, the <f:> rule)."""
        with pytest.raises(MarkupError) as excinfo:
            parse_markup("<tc:>x</tc>")
        assert "text color tag argument is empty" in str(excinfo.value)


def _raise_unknown(name: str) -> str:
    """Color resolver stub that rejects every name (the unknown-color contract)."""
    raise ValueError(f"{name!r} is not a known color")


class TestTextColorParse:
    """Parsing the <tc:color> tag (the inline stroke-color layer)."""

    def test_color_run_and_values(self) -> None:
        """The run carries the color and the per-char values align."""
        styled = parse_markup("A<tc:red>B</tc>C", color_resolver=str)
        assert styled.plain_text == "ABC"
        assert styled.text_color_values == (None, "red", None)

    def test_bare_color_tag_is_literal_text(self) -> None:
        """The color is mandatory, so a bare <tc> is not markup (the <scale> rule)."""
        styled = parse_markup("a <tc> b")
        assert styled.plain_text == "a <tc> b"
        assert styled.has_markup is False
        with pytest.raises(MarkupError, match="Unexpected closing tag"):
            parse_markup("<tc>text</tc>")

    def test_resolver_canonicalizes_at_open_time(self) -> None:
        """The stack stores the canonical name (the font_resolver precedent)."""

        def resolver(name: str) -> str:
            return {"r": "red", "b": "blue"}.get(name.lower(), name.lower())

        styled = parse_markup("<tc:R>x<tc:b>y</tc>z</tc>", color_resolver=resolver)
        assert styled.text_color_values == ("red", "blue", "red")

    def test_resolver_error_is_wrapped_with_position(self) -> None:
        """A resolver ValueError becomes a MarkupError naming the tag (fail-loud)."""
        with pytest.raises(MarkupError) as excinfo:
            parse_markup("A<tc:mauve>B</tc>", color_resolver=_raise_unknown)
        assert "Invalid tag <tc:mauve>" in str(excinfo.value)
        assert "position 1" in str(excinfo.value)

    def test_no_resolver_keeps_the_raw_argument(self) -> None:
        """Direct API callers without a resolver keep the decoded argument."""
        styled = parse_markup("<tc:r>x</tc>")
        assert styled.text_color_values == ("r",)

    def test_crossing_color_span_is_the_intersection(self) -> None:
        """A <tc> span crossing an <i> span composes per character (HTML rule)."""
        styled = parse_markup("<i>AB<tc:red>CD</tc>EF</i>", color_resolver=str)
        assert styled.italic_flags == (True,) * 6
        assert styled.text_color_values == (None, None, "red", "red", None, None)

    def test_the_color_is_independent_of_the_other_tags(self) -> None:
        """<tc:color> is its own field (never confused with <c:D> or <f:>)."""
        styled = parse_markup("<tc:red><c:0.06>x</c></tc>", color_resolver=str)
        assert styled.text_color_values == ("red",)
        assert styled.cutter_values == (0.06,)


class TestTextColorSerialize:
    """The <tc:color> serialization round-trip (the reflow depends on it)."""

    def test_serialized_form_reparses_identically(self) -> None:
        """serialize_runs -> parse_markup reproduces the same per-char values."""
        styled = parse_markup("A<tc:red>B</tc>C", color_resolver=str)
        raw = serialize_runs(styled.runs)
        assert raw == "A<tc:red>B</tc>C"
        reparsed = parse_markup(raw, color_resolver=str)
        assert reparsed.text_color_values == styled.text_color_values

    def test_nested_color_restores_the_outer_value(self) -> None:
        """A nested <tc:color> restores the outer color for the trailing text."""
        styled = parse_markup("<tc:red>a<tc:blue>b</tc>c</tc>", color_resolver=str)
        assert styled.text_color_values == ("red", "blue", "red")
        raw = serialize_runs(styled.runs)
        reparsed = parse_markup(raw, color_resolver=str)
        assert reparsed.text_color_values == styled.text_color_values

    def test_style_boundaries_see_the_color_change(self) -> None:
        """A color span's edges are style boundaries (the reflow's guard)."""
        styled = parse_markup("ab<tc:red>cd</tc>ef", color_resolver=str)
        assert style_boundaries(styled) == (2, 4)


class TestTextColorWords:
    """The <tc:color> reflow tokens (the line-content reflow round-trip)."""

    def test_styled_word_keeps_the_color_argument(self) -> None:
        """A styled word token round-trips with its color."""
        styled = parse_markup("big <tc:red>cut</tc>", color_resolver=str)
        words = split_styled_words(styled)
        assert [word.text for word in words] == ["big", "cut"]
        assert serialize_word(words[1]) == "<tc:red>cut</tc>"
        assert serialize_word(words[0]) == "big"

    def test_space_spanning_color_run_keeps_the_separator(self) -> None:
        """<tc:X>foo bar</tc> tokenizes with the color separator (underline rule)."""
        styled = parse_markup("<tc:red>foo bar</tc>", color_resolver=str)
        words, separators = split_styled_words_with_separators(styled)
        assert [word.text for word in words] == ["foo", "bar"]
        assert all(sep.text_color == "red" for sep in separators)


CUTTER_BUFFER = Style(cutter_buffer=0.015)


class TestCutterBufferRegistry:
    """The <cb:D> registry entry (the inline kerf-buffer value tag)."""

    def test_cutter_buffer_is_registered_with_args(self) -> None:
        """The tag accepts a mandatory arg and is registered LAST."""
        spec = TAG_REGISTRY["cb"]
        assert spec.accepts_args is True
        assert spec.args_optional is False
        assert list(TAG_REGISTRY).index("cb") == list(TAG_REGISTRY).index("tc") + 1
        assert list(TAG_REGISTRY)[-1] == "cb"
        assert spec.apply(PLAIN, "0.015") == CUTTER_BUFFER
        # Set-to-value: re-applying with the same argument is idempotent.
        assert spec.apply(CUTTER_BUFFER, "0.015") == CUTTER_BUFFER
        # A new argument replaces the active buffer.
        assert spec.apply(CUTTER_BUFFER, "0.030") == Style(cutter_buffer=0.030)
        # Applying <cb> inside <i> composes.
        assert spec.apply(Style(italic=True), "0.015") == Style(italic=True, cutter_buffer=0.015)

    def test_is_active_predicate(self) -> None:
        """The tag is active exactly when a buffer override is set."""
        spec = TAG_REGISTRY["cb"]
        assert spec.is_active(PLAIN) is False
        assert spec.is_active(CUTTER_BUFFER) is True

    def test_arg_serializer_round_trip(self) -> None:
        """The arg serializer emits the canonical argument the parser accepts."""
        spec = TAG_REGISTRY["cb"]
        assert spec.arg is not None
        assert spec.arg(CUTTER_BUFFER) == "0.015"
        assert spec.apply(PLAIN, spec.arg(CUTTER_BUFFER)) == CUTTER_BUFFER

    def test_zero_buffer_is_valid(self) -> None:
        """0.0 is a valid argument -- it explicitly overrides the line buffer to zero."""
        spec = TAG_REGISTRY["cb"]
        zero = Style(cutter_buffer=0.0)
        assert spec.apply(PLAIN, "0") == zero
        assert spec.is_active(zero) is True

    def test_cutter_buffer_is_independent_of_the_other_tags(self) -> None:
        """<cb:D> is its own field (never confused with <c:D> or <scale:N>)."""
        styled = parse_markup("<cb:0.015><scale:0.5>x</scale></cb>")
        assert styled.cutter_buffer_values == (0.015,)
        assert styled.scale_values == (0.5,)
        assert styled.cutter_values == (None,)


class TestCutterBufferParse:
    """Parsing the <cb:D> tag (the inline kerf buffer)."""

    def test_buffer_run_and_values(self) -> None:
        """The run carries the buffer and the per-char values align."""
        styled = parse_markup("A<cb:0.015>B</cb>C")
        assert styled.plain_text == "ABC"
        assert styled.cutter_buffer_values == (None, 0.015, None)

    def test_bare_buffer_is_literal_text(self) -> None:
        """The buffer value is mandatory, so a bare <cb> is not markup (the <c:D> rule).

        A bare open tag is literal text; the matching close then has no open
        to close, which is the framework's hard error for a mandatory-arg tag.
        """
        styled = parse_markup("a <cb> b")
        assert styled.plain_text == "a <cb> b"
        assert styled.has_markup is False
        with pytest.raises(MarkupError, match="Unexpected closing tag"):
            parse_markup("<cb>text</cb>")

    def test_non_numeric_argument_is_an_error(self) -> None:
        """A non-numeric buffer is broken markup (fail-loud)."""
        with pytest.raises(MarkupError) as excinfo:
            parse_markup("<cb:abc>x</cb>")
        assert "not a number" in str(excinfo.value)

    def test_empty_argument_is_an_error(self) -> None:
        """An empty buffer is broken markup (fail-loud)."""
        with pytest.raises(MarkupError) as excinfo:
            parse_markup("<cb:>x</cb>")
        assert "not a number" in str(excinfo.value)

    def test_zero_argument_is_valid(self) -> None:
        """Zero is a valid buffer (explicitly overrides the line buffer to zero)."""
        styled = parse_markup("A<cb:0>B</cb>C")
        assert styled.cutter_buffer_values == (None, 0.0, None)

    def test_negative_argument_is_an_error(self) -> None:
        """A negative buffer is outside the supported range."""
        with pytest.raises(MarkupError) as excinfo:
            parse_markup("<cb:-0.01>x</cb>")
        assert "outside the supported range" in str(excinfo.value)

    def test_oversized_argument_is_an_error(self) -> None:
        """A buffer above the ceiling is outside the supported range."""
        with pytest.raises(MarkupError) as excinfo:
            parse_markup(f"<cb:{MAX_BUFFER_ARG + 0.1}>x</cb>")
        assert "outside the supported range" in str(excinfo.value)

    def test_min_buffer_is_accepted(self) -> None:
        """The minimum bound (0.0) is exactly accepted."""
        styled = parse_markup(f"<cb:{MIN_BUFFER_ARG}>x</cb>")
        assert styled.cutter_buffer_values == (0.0,)

    def test_max_buffer_is_accepted(self) -> None:
        """The maximum bound is exactly accepted."""
        styled = parse_markup(f"<cb:{MAX_BUFFER_ARG}>x</cb>")
        assert styled.cutter_buffer_values == (MAX_BUFFER_ARG,)

    def test_crossing_buffer_span_is_the_intersection(self) -> None:
        """Crossing <cb>/<i> spans compose per-character like every other tag."""
        styled = parse_markup("<cb:0.015>A<i>B</i>C</cb>")
        assert styled.plain_text == "ABC"
        assert styled.cutter_buffer_values == (0.015, 0.015, 0.015)
        assert styled.italic_flags == (False, True, False)


class TestCutterBufferSerialize:
    """The <cb:D> serialization round-trip (the reflow depends on it)."""

    def test_serialized_form_reparses_identically(self) -> None:
        """serialize_runs -> parse_markup reproduces the same per-char values."""
        styled = parse_markup("A<cb:0.015>B</cb>C")
        raw = serialize_runs(styled.runs)
        assert raw == "A<cb:0.015>B</cb>C"
        assert parse_markup(raw).cutter_buffer_values == styled.cutter_buffer_values

    def test_nested_buffer_restores_the_outer_value(self) -> None:
        """A nested <cb:D> restores the outer buffer for the trailing text."""
        styled = parse_markup("<cb:0.015>a<cb:0.030>b</cb>c</cb>")
        assert styled.cutter_buffer_values == (0.015, 0.030, 0.015)
        raw = serialize_runs(styled.runs)
        assert parse_markup(raw).cutter_buffer_values == styled.cutter_buffer_values

    def test_style_boundaries_see_the_buffer_change(self) -> None:
        """A buffer span's edges are style boundaries (the reflow's guard)."""
        styled = parse_markup("ab<cb:0.015>cd</cb>ef")
        assert style_boundaries(styled) == (2, 4)


class TestCutterBufferWords:
    """The <cb:D> reflow tokens (the line-content reflow round-trip)."""

    def test_styled_word_keeps_the_buffer_argument(self) -> None:
        """A styled word token round-trips with its buffer."""
        styled = parse_markup("big <cb:0.015>buf</cb>")
        words = split_styled_words(styled)
        assert [word.text for word in words] == ["big", "buf"]
        assert serialize_word(words[1]) == "<cb:0.015>buf</cb>"
        assert serialize_word(words[0]) == "big"

    def test_space_spanning_buffer_run_keeps_the_separator(self) -> None:
        """<cb:X>foo bar</cb> tokenizes with the buffer separator (underline rule)."""
        styled = parse_markup("<cb:0.015>foo bar</cb>")
        words, separators = split_styled_words_with_separators(styled)
        assert [word.text for word in words] == ["foo", "bar"]
        assert all(sep.cutter_buffer == 0.015 for sep in separators)
