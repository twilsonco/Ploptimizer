"""Tests for font discovery and name resolution (``font_registry``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from plt_optimizer.generate import font_registry
from plt_optimizer.generate.font_registry import (
    DEFAULT_FONT_NAME,
    FONTS_DIR,
    PLT_FONTS_JSON_PATH,
    FontNotFoundError,
    PltFontData,
    available_ttf_fonts,
    find_italic_face_path,
    font_name_choices,
    load_plt_fonts,
    normalize_font_name,
    resolve_font,
)


def _v2_font(characters: Dict[str, str], **metadata: Any) -> Dict[str, Any]:
    """Build a v2 font object from a ``{char: hpgl}`` mapping.

    Args:
        characters: Character -> HPGL string mapping (bbox/envelope-less
            entries are valid v2 entries).
        **metadata: Optional metadata overrides (e.g. ``reference_char``).

    Returns:
        A JSON-serializable v2 font object.
    """
    font: Dict[str, Any] = {
        "reference_char": "A",
        "declared_height_in": 1.0,
        "reference_char_height_in": 1.0,
        "normalized_ref_height": 1.0,
        "characters": {char: {"glyph": hpgl} for char, hpgl in characters.items()},
    }
    font.update(metadata)
    return font


@pytest.fixture(autouse=True)
def _clear_caches() -> object:
    """Isolate the module's lru_caches between tests."""
    load_plt_fonts.cache_clear()
    available_ttf_fonts.cache_clear()
    find_italic_face_path.cache_clear()
    yield
    load_plt_fonts.cache_clear()
    available_ttf_fonts.cache_clear()
    find_italic_face_path.cache_clear()


@pytest.fixture
def fonts_root(tmp_path: Path) -> Path:
    """A synthetic Fonts root with one PLT library and two TTFs."""
    (tmp_path / "Sub").mkdir()
    (tmp_path / "Alpha.ttf").write_bytes(b"ttf-a")
    (tmp_path / "Sub" / "Beta.ttf").write_bytes(b"ttf-b")
    (tmp_path / "ignored.otf").write_bytes(b"otf")
    library = tmp_path / "plt_fonts.json"
    library.write_text(
        json.dumps(
            {
                "Dino": _v2_font({"A": "PU0,0;PD1,1;"}),
                "Jhanuni": _v2_font({"A": "PU0,0;"}),
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


class TestBundledAssets:
    """The shipped repository assets must be discoverable."""

    def test_paths_point_at_repo_fonts_dir(self) -> None:
        """The module resolves the real Fonts/ directory."""
        assert FONTS_DIR.name == "Fonts"
        assert FONTS_DIR.is_dir()
        assert PLT_FONTS_JSON_PATH == FONTS_DIR / "plt_fonts.json"
        assert PLT_FONTS_JSON_PATH.exists()

    def test_bundled_library_loads(self) -> None:
        """The shipped plt_fonts.json parses into v2 font objects."""
        fonts = load_plt_fonts()
        assert fonts, "bundled plt_fonts.json produced no fonts"
        for name, data in fonts.items():
            assert name
            assert isinstance(data, PltFontData)
            assert data.characters, f"font {name!r} carries no characters"
            for char, entry in data.characters.items():
                assert isinstance(char, str)
                assert isinstance(entry.glyph, str)
            assert data.normalized_ref_height > 0.0

    def test_default_font_is_available(self) -> None:
        """The default font name resolves to the bundled TTF."""
        ref = resolve_font(DEFAULT_FONT_NAME)
        assert ref.kind == "ttf"
        assert ref.path is not None and ref.path.exists()
        assert ref.path.name == f"{DEFAULT_FONT_NAME}.ttf"

    def test_choices_include_both_families(self) -> None:
        """The valid-name list contains PLT keys and TTF basenames."""
        choices = font_name_choices()
        assert "Dino" in choices
        assert DEFAULT_FONT_NAME in choices
        assert choices == sorted(choices, key=str.lower)


class TestLoadPltFonts:
    """PLT library loading and its degradation paths."""

    def test_loads_glyph_maps(self, fonts_root: Path) -> None:
        """A well-formed library yields name -> v2 font data."""
        fonts = load_plt_fonts(fonts_root / "plt_fonts.json")
        assert set(fonts) == {"Dino", "Jhanuni"}
        assert fonts["Dino"].characters["A"].glyph == "PU0,0;PD1,1;"
        assert fonts["Dino"].reference_char == "A"
        assert fonts["Dino"].normalized_ref_height == pytest.approx(1.0)

    def test_parses_bbox_and_envelopes(self, tmp_path: Path) -> None:
        """Nested bbox/envelope structures land on the glyph entry."""
        library = tmp_path / "plt_fonts.json"
        library.write_text(
            json.dumps(
                {
                    "Test": {
                        "normalized_ref_height": 1.0,
                        "characters": {
                            "A": {
                                "glyph": "PU0,0;PD1000,1000;",
                                "bounding_box": {
                                    "min_x": 0.0,
                                    "min_y": 0.0,
                                    "max_x": 1000.0,
                                    "max_y": 1000.0,
                                },
                                "left_envelope": [[0.0, 0.0], [1000.0, 1000.0]],
                                "right_envelope": [[0.0, 0.0], [1000.0, 1000.0]],
                            }
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        entry = load_plt_fonts(library)["Test"].characters["A"]
        assert entry.bounding_box == (0.0, 0.0, 1000.0, 1000.0)
        assert entry.left_envelope == ((0.0, 0.0), (1000.0, 1000.0))
        assert entry.right_envelope == ((0.0, 0.0), (1000.0, 1000.0))

    def test_legacy_flat_font_skipped_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A legacy flat ``char -> HPGL`` font is rejected with a WARNING."""
        library = tmp_path / "plt_fonts.json"
        library.write_text(
            json.dumps({"Old": {"A": "PU0,0;"}, "New": _v2_font({"A": "PU0,0;"})}),
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            fonts = load_plt_fonts(library)
        assert set(fonts) == {"New"}
        assert "legacy flat" in caplog.text

    def test_malformed_glyph_entry_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A character entry without a glyph string is skipped."""
        library = tmp_path / "plt_fonts.json"
        library.write_text(
            json.dumps({"Test": _v2_font({"A": "PU0,0;", "B": {"bounding_box": {"min_x": 0}}})}),
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            fonts = load_plt_fonts(library)
        assert set(fonts["Test"].characters) == {"A"}
        assert "not a v2 glyph entry" in caplog.text

    def test_non_positive_normalized_height_falls_back(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A bogus normalized_ref_height warns and falls back to 1.0."""
        library = tmp_path / "plt_fonts.json"
        library.write_text(
            json.dumps({"Test": _v2_font({"A": "PU0,0;"}, normalized_ref_height=0.0)}),
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            fonts = load_plt_fonts(library)
        assert fonts["Test"].normalized_ref_height == pytest.approx(1.0)
        assert "non-positive" in caplog.text

    def test_missing_file_warns_and_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing library must not raise (TTF-only jobs keep working)."""
        with caplog.at_level("WARNING"):
            assert load_plt_fonts(tmp_path / "nope.json") == {}
        assert "not found" in caplog.text

    def test_malformed_json_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Unparseable JSON degrades to no PLT fonts."""
        bad = tmp_path / "plt_fonts.json"
        bad.write_text("{not json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            assert load_plt_fonts(bad) == {}
        assert "could not be read" in caplog.text

    def test_non_object_root_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A JSON array root is rejected with a WARNING."""
        bad = tmp_path / "plt_fonts.json"
        bad.write_text("[1, 2]", encoding="utf-8")
        with caplog.at_level("WARNING"):
            assert load_plt_fonts(bad) == {}
        assert "not a JSON object" in caplog.text

    def test_non_map_font_entry_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A font entry that is not a v2 object is skipped."""
        bad = tmp_path / "plt_fonts.json"
        bad.write_text(json.dumps({"Good": _v2_font({"A": "x"}), "Bad": [1, 2]}), encoding="utf-8")
        with caplog.at_level("WARNING"):
            fonts = load_plt_fonts(bad)
        assert set(fonts) == {"Good"}
        assert "Bad" in caplog.text

    def test_stroke_count_defaults_to_one(self, tmp_path: Path) -> None:
        """A library written before the multi-stroke convention loads as 1."""
        library = tmp_path / "plt_fonts.json"
        library.write_text(json.dumps({"Test": _v2_font({"A": "PU0,0;"})}), encoding="utf-8")
        assert load_plt_fonts(library)["Test"].stroke_count == 1

    def test_stroke_count_parsed(self, tmp_path: Path) -> None:
        """The extractor's stroke_count metadata lands on the font data."""
        library = tmp_path / "plt_fonts.json"
        library.write_text(
            json.dumps({"Test": _v2_font({"A": "PU0,0;"}, stroke_count=5)}),
            encoding="utf-8",
        )
        assert load_plt_fonts(library)["Test"].stroke_count == 5

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("5", 5), (5.0, 5), (0, 1), (-3, 1), ("five", 1), (True, 1), (None, 1)],
    )
    def test_stroke_count_lenient_parse(
        self, tmp_path: Path, raw: object, expected: int
    ) -> None:
        """Garbage/stale stroke_count values fall back to single-stroke."""
        library = tmp_path / "plt_fonts.json"
        library.write_text(
            json.dumps({"Test": _v2_font({"A": "PU0,0;"}, stroke_count=raw)}),
            encoding="utf-8",
        )
        assert load_plt_fonts(library)["Test"].stroke_count == expected

    def test_stroke_count_garbage_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A malformed stroke_count logs a WARNING and falls back to 1."""
        library = tmp_path / "plt_fonts.json"
        library.write_text(
            json.dumps({"Test": _v2_font({"A": "PU0,0;"}, stroke_count="five")}),
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            fonts = load_plt_fonts(library)
        assert fonts["Test"].stroke_count == 1
        assert "stroke_count" in caplog.text


class TestPltFontStrokeCount:
    """The resolution-time stroke-count lookup."""

    @pytest.fixture
    def stroke_lib(self, tmp_path: Path) -> Path:
        """A library with a single-stroke and a five-stroke PLT font."""
        library = tmp_path / "plt_fonts.json"
        library.write_text(
            json.dumps(
                {
                    "Dino": _v2_font({"A": "PU0,0;"}),
                    "Avenge5": _v2_font({"A": "PU0,0;"}, stroke_count=5),
                }
            ),
            encoding="utf-8",
        )
        return library

    def test_multi_stroke_font_found(self, stroke_lib: Path) -> None:
        assert font_registry.plt_font_stroke_count("Avenge5", stroke_lib) == 5

    def test_single_stroke_font_defaults(self, stroke_lib: Path) -> None:
        assert font_registry.plt_font_stroke_count("Dino", stroke_lib) == 1

    def test_lookup_is_case_insensitive(self, stroke_lib: Path) -> None:
        assert font_registry.plt_font_stroke_count("avenge5", stroke_lib) == 5

    def test_small_caps_variant_strips_suffix(self, stroke_lib: Path) -> None:
        assert font_registry.plt_font_stroke_count("Avenge5 SC", stroke_lib) == 5

    def test_unknown_font_is_single_stroke(self, stroke_lib: Path) -> None:
        assert font_registry.plt_font_stroke_count("Nope", stroke_lib) == 1

    def test_ttf_font_is_single_stroke(self, fonts_root: Path) -> None:
        """TTF names are not PLT library keys -> single-stroke."""
        assert (
            font_registry.plt_font_stroke_count("Alpha", fonts_root / "plt_fonts.json") == 1
        )

    def test_blank_name_is_single_stroke(self, stroke_lib: Path) -> None:
        assert font_registry.plt_font_stroke_count("  ", stroke_lib) == 1

    def test_missing_library_is_single_stroke(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing library degrades (WARNING from the loader) to 1."""
        with caplog.at_level("WARNING"):
            assert font_registry.plt_font_stroke_count("Avenge5", tmp_path / "nope.json") == 1


class TestAvailableTtfFonts:
    """TrueType discovery under the Fonts tree."""

    def test_discovers_recursively_by_basename(self, fonts_root: Path) -> None:
        """Nested TTFs are keyed by stem; non-TTF extensions are ignored."""
        found = available_ttf_fonts(fonts_root)
        assert set(found) == {"Alpha", "Beta"}
        assert found["Beta"] == fonts_root / "Sub" / "Beta.ttf"

    def test_missing_directory_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A non-existent Fonts root yields no fonts."""
        with caplog.at_level("WARNING"):
            assert available_ttf_fonts(tmp_path / "nope") == {}
        assert "does not exist" in caplog.text

    def test_collision_prefers_first_path(self, tmp_path: Path) -> None:
        """Duplicate basenames resolve deterministically (first path wins)."""
        (tmp_path / "a").mkdir()
        (tmp_path / "z").mkdir()
        (tmp_path / "a" / "Same.ttf").write_bytes(b"1")
        (tmp_path / "z" / "Same.ttf").write_bytes(b"2")
        assert available_ttf_fonts(tmp_path)["Same"] == tmp_path / "a" / "Same.ttf"


class TestResolveFont:
    """Case-insensitive resolution and error reporting."""

    def test_ttf_exact(self, fonts_root: Path) -> None:
        """A TTF basename resolves with its file path."""
        ref = resolve_font("Alpha", fonts_dir=fonts_root)
        assert (ref.kind, ref.name) == ("ttf", "Alpha")
        assert ref.path == fonts_root / "Alpha.ttf"

    def test_plt_exact(self, fonts_root: Path) -> None:
        """A PLT key resolves without a path."""
        ref = resolve_font("Dino", json_path=fonts_root / "plt_fonts.json", fonts_dir=fonts_root)
        assert (ref.kind, ref.name) == ("plt", "Dino")
        assert ref.path is None

    @pytest.mark.parametrize("requested", ["dino", "DINO", "DiNo"])
    def test_case_insensitive(self, fonts_root: Path, requested: str) -> None:
        """Any casing resolves to the canonical name."""
        ref = resolve_font(requested, json_path=fonts_root / "plt_fonts.json", fonts_dir=fonts_root)
        assert ref.name == "Dino"

    def test_surrounding_whitespace_tolerated(self, fonts_root: Path) -> None:
        """Padded names still resolve."""
        assert resolve_font("  Alpha  ", fonts_dir=fonts_root).name == "Alpha"

    def test_ttf_wins_cross_family_collision(self, tmp_path: Path) -> None:
        """A TTF basename shadowing a PLT key resolves to the TTF."""
        library = tmp_path / "plt_fonts.json"
        library.write_text(json.dumps({"Dup": _v2_font({"A": "x"})}), encoding="utf-8")
        (tmp_path / "Dup.ttf").write_bytes(b"t")
        ref = resolve_font("dup", json_path=library, fonts_dir=tmp_path)
        assert ref.kind == "ttf"

    def test_unknown_lists_choices(self, fonts_root: Path) -> None:
        """An unknown name raises with the full valid list."""
        with pytest.raises(FontNotFoundError) as excinfo:
            resolve_font("Nope", json_path=fonts_root / "plt_fonts.json", fonts_dir=fonts_root)
        message = str(excinfo.value)
        assert "Nope" in message
        assert "Dino" in message and "Alpha" in message
        assert excinfo.value.requested == "Nope"

    @pytest.mark.parametrize("requested", ["", "   "])
    def test_blank_rejected(self, fonts_root: Path, requested: str) -> None:
        """Blank names never match."""
        with pytest.raises(FontNotFoundError):
            resolve_font(requested, fonts_dir=fonts_root)

    def test_normalize_returns_canonical_name(self, fonts_root: Path) -> None:
        """The schema-facing helper returns just the name."""
        assert (
            normalize_font_name(
                "jhanuni", json_path=fonts_root / "plt_fonts.json", fonts_dir=fonts_root
            )
            == "Jhanuni"
        )


class TestSmallCapsVariants:
    """Synthetic ``"<base> SC"`` small-caps variants."""

    def test_plt_variant_resolves_to_base_family(self, fonts_root: Path) -> None:
        """A PLT variant keeps the PLT kind, carries no path, and is flagged."""
        ref = resolve_font("Dino SC", json_path=fonts_root / "plt_fonts.json", fonts_dir=fonts_root)
        assert (ref.kind, ref.name, ref.path, ref.small_caps) == ("plt", "Dino SC", None, True)

    def test_ttf_variant_carries_base_path(self, fonts_root: Path) -> None:
        """A TTF variant resolves to the TTF family with the BASE file path."""
        ref = resolve_font("Alpha SC", fonts_dir=fonts_root)
        assert (ref.kind, ref.name, ref.small_caps) == ("ttf", "Alpha SC", True)
        assert ref.path == fonts_root / "Alpha.ttf"

    @pytest.mark.parametrize("requested", ["dino sc", "DINO SC", "DiNo Sc", " dino sc "])
    def test_variant_matching_is_case_insensitive(self, fonts_root: Path, requested: str) -> None:
        """Any casing/whitespace of the variant name resolves canonically."""
        ref = resolve_font(requested, json_path=fonts_root / "plt_fonts.json", fonts_dir=fonts_root)
        assert ref.name == "Dino SC"
        assert ref.small_caps is True

    def test_base_font_is_not_flagged(self, fonts_root: Path) -> None:
        """A base name never reports small_caps."""
        ref = resolve_font("Dino", json_path=fonts_root / "plt_fonts.json", fonts_dir=fonts_root)
        assert ref.small_caps is False

    def test_choices_include_variants(self, fonts_root: Path) -> None:
        """Every base contributes a variant to the valid-name list."""
        choices = font_name_choices(json_path=fonts_root / "plt_fonts.json", fonts_dir=fonts_root)
        assert {"Dino SC", "Jhanuni SC", "Alpha SC", "Beta SC"} <= set(choices)
        assert choices == sorted(choices, key=str.lower)

    def test_real_sc_named_font_wins_over_synthetic(self, tmp_path: Path) -> None:
        """A real TTF literally named ``Foo SC`` beats the synthetic variant."""
        (tmp_path / "Foo SC.ttf").write_bytes(b"t")
        library = tmp_path / "plt_fonts.json"
        library.write_text(json.dumps({"Foo": _v2_font({"A": "x"})}), encoding="utf-8")
        ref = resolve_font("Foo SC", json_path=library, fonts_dir=tmp_path)
        assert ref.kind == "ttf"
        # The real file is a base font (its name merely ends in SC), so it is
        # not a synthetic variant.
        assert ref.small_caps is False

    @pytest.mark.parametrize(
        ("requested", "expected"),
        [
            ("Dino SC", "Dino"),
            ("Dino", "Dino"),
            ("Dino SC SC", "Dino SC"),
            ("dino sc", "dino"),
        ],
    )
    def test_base_font_name_strips_one_suffix(self, requested: str, expected: str) -> None:
        """The library-key helper strips exactly one trailing `` SC``."""
        assert font_registry.base_font_name(requested) == expected

    def test_bundled_variants_resolve(self) -> None:
        """The shipped fonts each expose a working ``SC`` variant."""
        for base in font_name_choices():
            if base.lower().endswith(" sc"):
                continue
            ref = resolve_font(f"{base} SC")
            assert ref.small_caps is True
            assert ref.kind == resolve_font(base).kind
            assert ref.path == resolve_font(base).path


class TestItalicFaceProbe:
    """The sibling-file probe for a TTF family's native italic face."""

    def test_none_path_yields_none(self) -> None:
        """A PLT font (no path) has no native italic face."""
        assert find_italic_face_path(None) is None

    def test_family_without_italic_yields_none(self, fonts_root: Path) -> None:
        """A family shipping only the regular face reports no italic."""
        assert find_italic_face_path(fonts_root / "Alpha.ttf") is None

    def test_hyphenated_italic_suffix(self, tmp_path: Path) -> None:
        """``Foo-Italic.ttf`` next to ``Foo.ttf`` is the convention."""
        regular = tmp_path / "Foo.ttf"
        regular.write_bytes(b"1")
        italic = tmp_path / "Foo-Italic.ttf"
        italic.write_bytes(b"2")
        assert find_italic_face_path(regular) == italic

    def test_oblique_suffix(self, tmp_path: Path) -> None:
        """``Foo-Oblique.ttf`` is the family's slanted cut."""
        regular = tmp_path / "Foo.ttf"
        regular.write_bytes(b"1")
        oblique = tmp_path / "Foo-Oblique.ttf"
        oblique.write_bytes(b"2")
        assert find_italic_face_path(regular) == oblique

    def test_concatenated_suffix(self, tmp_path: Path) -> None:
        """``FooItalic.ttf`` (no separator) also matches."""
        regular = tmp_path / "Foo.ttf"
        regular.write_bytes(b"1")
        italic = tmp_path / "FooItalic.ttf"
        italic.write_bytes(b"2")
        assert find_italic_face_path(regular) == italic

    def test_probe_is_case_insensitive(self, tmp_path: Path) -> None:
        """The stem match ignores case (``FOO-ITALIC.TTF``)."""
        regular = tmp_path / "Foo.ttf"
        regular.write_bytes(b"1")
        italic = tmp_path / "FOO-ITALIC.TTF"
        italic.write_bytes(b"2")
        assert find_italic_face_path(regular) == italic

    def test_unrelated_sibling_is_not_italic(self, tmp_path: Path) -> None:
        """A neighbouring family's italic face is never picked up."""
        regular = tmp_path / "Foo.ttf"
        regular.write_bytes(b"1")
        (tmp_path / "Bar-Italic.ttf").write_bytes(b"2")
        assert find_italic_face_path(regular) is None

    def test_resolve_font_populates_italic_path(self, tmp_path: Path) -> None:
        """The resolved reference carries the probed face for a TTF."""
        regular = tmp_path / "Alpha.ttf"
        regular.write_bytes(b"1")
        italic = tmp_path / "Alpha-Italic.ttf"
        italic.write_bytes(b"2")
        ref = resolve_font("Alpha", fonts_dir=tmp_path)
        assert ref.italic_path == italic

    def test_resolve_font_italic_path_none_without_face(self, fonts_root: Path) -> None:
        """A family with no italic face resolves ``italic_path=None``."""
        ref = resolve_font("Alpha", fonts_dir=fonts_root)
        assert ref.italic_path is None

    def test_small_caps_variant_carries_italic_path(self, tmp_path: Path) -> None:
        """The SC variant carries the base family's probed italic face."""
        regular = tmp_path / "Alpha.ttf"
        regular.write_bytes(b"1")
        italic = tmp_path / "Alpha-Italic.ttf"
        italic.write_bytes(b"2")
        ref = resolve_font("Alpha SC", fonts_dir=tmp_path)
        assert ref.small_caps is True
        assert ref.italic_path == italic

    def test_plt_font_has_no_italic_path(self, fonts_root: Path) -> None:
        """PLT fonts always take the shear path (no TTF file to probe)."""
        ref = resolve_font("Dino", json_path=fonts_root / "plt_fonts.json", fonts_dir=fonts_root)
        assert ref.italic_path is None
