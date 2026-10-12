"""Tests for the inline markup parser (:mod:`plt_optimizer.generate.markup`).

Covers the framework's contract: literal-first scanning, configurable
multi-character delimiters, hard errors on broken markup, the run/word
partitions the renderers and the reflow consume, and the serialize round-trip.
"""

from __future__ import annotations

import pytest

from plt_optimizer.generate.markup import (
    PLAIN,
    TAG_REGISTRY,
    MarkupError,
    Run,
    Style,
    parse_markup,
    register_tag,
    serialize_runs,
    serialize_word,
    split_brackets,
    split_styled_words,
    style_boundaries,
)

ITALIC = Style(italic=True)


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
        # Only <i> is registered, so </b> is literal; the <i> stays unclosed.
        with pytest.raises(MarkupError, match="Unclosed tag"):
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
        assert spec.apply(PLAIN) == ITALIC
        assert spec.apply(ITALIC) == ITALIC
        assert spec.accepts_args is False

    def test_register_tag_rejects_bad_name(self) -> None:
        """Tag names are ASCII letters only."""
        with pytest.raises(ValueError, match="ASCII letters"):
            register_tag("bad-name", lambda style: style)

    def test_register_tag_round_trip(self) -> None:
        """A registered tag parses like the shipped one.

        Serialization emits the *first* registry tag whose ``apply`` is
        active (registry order fixes nesting), so a tag mapping to the same
        style as ``i`` serializes as ``<i>`` -- the round-trip of the parse
        is what the reflow relies on.
        """
        original = dict(TAG_REGISTRY)
        try:
            register_tag("zz", lambda style: Style(italic=True))
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
