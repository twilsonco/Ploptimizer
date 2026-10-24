"""Tests for plt_optimizer.generate.preview_colors.

Covers the material-name color conventions (character, delimiter, and
full-name forms), the stroke-color precedence chain, and the dark-background
auto-contrast fallback used by the colored combined ``all_*.pdf`` proofs.
"""

from __future__ import annotations

import pytest

from plt_optimizer.generate.preview_colors import (
    PREVIEW_COLOR_ABBREVIATIONS,
    PREVIEW_COLOR_NAMES,
    MaterialColors,
    parse_material_colors,
    resolve_text_color,
)


class TestPalette:
    """The preview palette extends TextColor with white."""

    def test_white_added_with_w_letter(self) -> None:
        """white/w exist for materials even though TextColor has no white."""
        assert "white" in PREVIEW_COLOR_NAMES
        assert PREVIEW_COLOR_ABBREVIATIONS["white"] == "w"

    def test_none_excluded(self) -> None:
        """The implicit 'none' layer tag is not a preview color."""
        assert "none" not in PREVIEW_COLOR_NAMES
        assert "none" not in PREVIEW_COLOR_ABBREVIATIONS

    def test_textcolor_names_present(self) -> None:
        """Every real TextColor name is a preview color."""
        from plt_optimizer.generate.schema import TextColor

        for member in TextColor:
            if member is TextColor.NONE:
                continue
            assert member.value in PREVIEW_COLOR_NAMES


class TestParseMaterialColorsCharacterForm:
    """Leading color-letter runs (the shop's bw/wb/wrb convention)."""

    def test_bw_is_black_over_white(self) -> None:
        """Two letters: top layer = background, core = text color."""
        assert parse_material_colors("bw") == MaterialColors(background="black", text="white")

    def test_wb_is_white_over_black(self) -> None:
        """The reverse spelling swaps background and text."""
        assert parse_material_colors("wb") == MaterialColors(background="white", text="black")

    def test_case_insensitive(self) -> None:
        """Comparison ignores case."""
        assert parse_material_colors("BW") == parse_material_colors("bw")
        assert parse_material_colors("Wb(uv)") == parse_material_colors("wb(uv)")

    def test_property_suffix_stops_the_letter_run(self) -> None:
        """Unrecognized letters after the colors are property tags, ignored."""
        assert parse_material_colors("bwuv") == MaterialColors(background="black", text="white")
        assert parse_material_colors("wb(uv)") == MaterialColors(background="white", text="black")
        assert parse_material_colors("bw-adhesive") == MaterialColors(
            background="black", text="white"
        )

    def test_three_letters_are_multilayer_background_only(self) -> None:
        """wrb = white/red/black: only the top layer (background) is certain."""
        assert parse_material_colors("wrb") == MaterialColors(background="white", text=None)
        assert parse_material_colors("wrb(uv)") == MaterialColors(background="white", text=None)

    def test_single_letter_is_background_only(self) -> None:
        """One letter names the top layer; the core is unknown."""
        assert parse_material_colors("b") == MaterialColors(background="black", text=None)
        assert parse_material_colors("w") == MaterialColors(background="white", text=None)
        assert parse_material_colors("m") == MaterialColors(background="magenta", text=None)

    def test_material_b_means_black_not_blue(self) -> None:
        """The material-side override: b = black (TextColor uses b = blue)."""
        assert parse_material_colors("b").background == "black"
        assert parse_material_colors("k").background == "black"
        assert parse_material_colors("kb") == MaterialColors(background="black", text="black")

    def test_unknown_material_yields_nothing(self) -> None:
        """Names with no recognized color prefix keep the black-on-white proof."""
        assert parse_material_colors("3101") == MaterialColors()
        assert parse_material_colors("acrylic") == MaterialColors()

    def test_empty_and_none(self) -> None:
        """Unset/blank materials parse to no colors."""
        assert parse_material_colors(None) == MaterialColors()
        assert parse_material_colors("") == MaterialColors()
        assert parse_material_colors("   ") == MaterialColors()

    def test_whitespace_trimmed(self) -> None:
        """Surrounding whitespace is ignored."""
        assert parse_material_colors("  bw  ") == parse_material_colors("bw")

    def test_known_limitation_color_letter_words(self) -> None:
        """A word built from color letters reads as layers (documented)."""
        assert parse_material_colors("gold") == MaterialColors(background="green", text="orange")


class TestParseMaterialColorsDelimiterForm:
    """Explicit background/text tokens joined by a delimiter."""

    @pytest.mark.parametrize("delimiter", ["-on-", ":", "|", "/"])
    def test_full_names(self, delimiter: str) -> None:
        """First token = background (top layer), second = text color."""
        material = f"red{delimiter}white"
        assert parse_material_colors(material) == MaterialColors(background="red", text="white")

    def test_letters(self) -> None:
        """Single-letter tokens work in the delimiter form too."""
        assert parse_material_colors("k-on-w") == MaterialColors(background="black", text="white")
        assert parse_material_colors("w:r") == MaterialColors(background="white", text="red")

    def test_trailing_property_suffix_ignored(self) -> None:
        """Everything after the two color tokens is a property tag."""
        assert parse_material_colors("red-on-white(uv)") == MaterialColors(
            background="red", text="white"
        )

    def test_unknown_background_token_falls_through(self) -> None:
        """An unrecognized first token defers to the other forms."""
        assert parse_material_colors("uv:white") == MaterialColors()

    def test_unknown_text_token_keeps_background(self) -> None:
        """An unrecognized second token leaves the text color unset."""
        assert parse_material_colors("red-on-uv") == MaterialColors(background="red")


class TestParseMaterialColorsFullNameForm:
    """Full color names as the material prefix."""

    def test_full_name_is_background_only(self) -> None:
        """'white' names the top layer; text color comes from text_color."""
        assert parse_material_colors("white") == MaterialColors(background="white")
        assert parse_material_colors("black(uv)") == MaterialColors(background="black")
        assert parse_material_colors("red adhesive") == MaterialColors(background="red")

    def test_longest_name_wins(self) -> None:
        """'blue...' matches blue, not the black/b letters alone."""
        assert parse_material_colors("blue") == MaterialColors(background="blue")
        assert parse_material_colors("blue-on-white") == MaterialColors(
            background="blue", text="white"
        )

    def test_violet_beats_violet_prefixes(self) -> None:
        """Full-name matching is longest-first and exact-prefix."""
        assert parse_material_colors("violet") == MaterialColors(background="violet")


class TestResolveTextColor:
    """The pen-color -> material-text -> black precedence chain."""

    def test_pen_color_wins_over_material_text(self) -> None:
        """An explicit text_color/<tc:> layer beats the material core."""
        mc = parse_material_colors("bw")  # text = white
        assert resolve_text_color("red", mc) == "red"
        assert resolve_text_color("cyan", mc) == "cyan"

    def test_pen_color_wins_over_material_text_wb(self) -> None:
        """Same on white-over-black stock (plain text would be black)."""
        mc = parse_material_colors("wb")
        assert resolve_text_color("magenta", mc) == "magenta"

    def test_none_pen_color_uses_material_text(self) -> None:
        """The implicit 'none' layer color defers to the material core."""
        assert resolve_text_color("none", parse_material_colors("bw")) == "white"
        assert resolve_text_color("none", parse_material_colors("wb")) == "black"

    def test_none_pen_color_on_three_layer_uses_contrast(self) -> None:
        """wrb: text unset, background white -> black ink."""
        assert resolve_text_color("none", parse_material_colors("wrb")) == "black"

    def test_dark_background_auto_contrast(self) -> None:
        """Unresolved text over a dark background renders white."""
        for material in ("b", "black", "red", "blue", "g", "m", "v"):
            mc = parse_material_colors(material)
            assert resolve_text_color(None, mc) == "white", material

    def test_light_background_keeps_black(self) -> None:
        """Unresolved text over a light background renders black."""
        for material in ("w", "white", "y", "o", "p"):
            mc = parse_material_colors(material)
            assert resolve_text_color(None, mc) == "black", material

    def test_no_material_is_black(self) -> None:
        """The historical proof: black ink when nothing is known."""
        assert resolve_text_color(None, MaterialColors()) == "black"
        assert resolve_text_color("none", MaterialColors()) == "black"

    def test_unknown_pen_color_falls_back(self) -> None:
        """A pen tag outside the palette defers like 'none'."""
        assert resolve_text_color("bogus", parse_material_colors("bw")) == "white"
        assert resolve_text_color("bogus", MaterialColors()) == "black"

    def test_pen_color_case_and_whitespace_tolerant(self) -> None:
        """Pen tags are trimmed and lower-cased before lookup."""
        assert resolve_text_color(" Red ", MaterialColors()) == "red"
