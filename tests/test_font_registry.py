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
    NON_TEXT_FONTS_FILE,
    PLT_FONT_CATALOG_DIR,
    FontNotFoundError,
    PltFontData,
    available_ttf_fonts,
    find_italic_face_path,
    font_name_choices,
    load_plt_fonts,
    non_text_font_names,
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


def _write_catalog(root: Path, fonts: Dict[str, Dict[str, Any]]) -> Path:
    """Write one JSON file per font into a catalog directory.

    Mirrors the extractor's layout: each file carries the font key as its
    ``font_name`` field, injected here in front of the given font object.

    Args:
        root: Catalog directory (created).
        fonts: Font name -> font object (as built by :func:`_v2_font`).

    Returns:
        The catalog directory.
    """
    root.mkdir(parents=True, exist_ok=True)
    for name, font in fonts.items():
        payload = {"font_name": name, **font}
        (root / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _clear_caches() -> object:
    """Isolate the module's lru_caches between tests."""
    load_plt_fonts.cache_clear()
    available_ttf_fonts.cache_clear()
    find_italic_face_path.cache_clear()
    non_text_font_names.cache_clear()
    yield
    load_plt_fonts.cache_clear()
    available_ttf_fonts.cache_clear()
    find_italic_face_path.cache_clear()
    non_text_font_names.cache_clear()


@pytest.fixture
def fonts_root(tmp_path: Path) -> Path:
    """A synthetic Fonts root with one PLT catalog and two TTFs."""
    (tmp_path / "Sub").mkdir()
    (tmp_path / "Alpha.ttf").write_bytes(b"ttf-a")
    (tmp_path / "Sub" / "Beta.ttf").write_bytes(b"ttf-b")
    (tmp_path / "ignored.otf").write_bytes(b"otf")
    _write_catalog(
        tmp_path / "VP-EL-Fonts" / "catalog",
        {
            "Dino": _v2_font({"A": "PU0,0;PD1,1;"}),
            "Jhanuni": _v2_font({"A": "PU0,0;"}),
        },
    )
    return tmp_path


class TestBundledAssets:
    """The shipped repository assets must be discoverable."""

    def test_paths_point_at_repo_fonts_dir(self) -> None:
        """The module resolves the real fonts/ directory."""
        assert FONTS_DIR.name == "Fonts"
        assert FONTS_DIR.is_dir()
        assert PLT_FONT_CATALOG_DIR == FONTS_DIR / "VP-EL-Fonts" / "catalog"
        assert PLT_FONT_CATALOG_DIR.is_dir()

    def test_bundled_library_loads(self) -> None:
        """The shipped catalog parses into v2 font objects."""
        fonts = load_plt_fonts()
        assert fonts, "bundled font catalog produced no fonts"
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
    """PLT font catalog loading and its degradation paths."""

    def test_loads_glyph_maps(self, fonts_root: Path) -> None:
        """A well-formed catalog yields name -> v2 font data."""
        fonts = load_plt_fonts(fonts_root / "VP-EL-Fonts" / "catalog")
        assert set(fonts) == {"Dino", "Jhanuni"}
        assert fonts["Dino"].characters["A"].glyph == "PU0,0;PD1,1;"
        assert fonts["Dino"].reference_char == "A"
        assert fonts["Dino"].normalized_ref_height == pytest.approx(1.0)

    def test_parses_bbox_and_envelopes(self, tmp_path: Path) -> None:
        """Nested bbox/envelope structures land on the glyph entry."""
        catalog = _write_catalog(
            tmp_path / "catalog",
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
            },
        )
        entry = load_plt_fonts(catalog)["Test"].characters["A"]
        assert entry.bounding_box == (0.0, 0.0, 1000.0, 1000.0)
        assert entry.left_envelope == ((0.0, 0.0), (1000.0, 1000.0))
        assert entry.right_envelope == ((0.0, 0.0), (1000.0, 1000.0))

    def test_legacy_flat_file_skipped_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A legacy flat ``char -> HPGL`` file (no font_name) is rejected."""
        catalog = tmp_path / "catalog"
        catalog.mkdir()
        (catalog / "Old.json").write_text(json.dumps({"A": "PU0,0;"}), encoding="utf-8")
        _write_catalog(catalog, {"New": _v2_font({"A": "PU0,0;"})})
        with caplog.at_level("WARNING"):
            fonts = load_plt_fonts(catalog)
        assert set(fonts) == {"New"}
        assert "font_name" in caplog.text

    def test_duplicate_font_name_first_file_wins(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Two files declaring one font_name: the sorted-first file wins."""
        catalog = tmp_path / "catalog"
        catalog.mkdir()
        (catalog / "a_first.json").write_text(
            json.dumps({"font_name": "Dup", **_v2_font({"A": "PU0,0;"})}), encoding="utf-8"
        )
        (catalog / "b_second.json").write_text(
            json.dumps({"font_name": "Dup", **_v2_font({"A": "PU9,9;"})}), encoding="utf-8"
        )
        with caplog.at_level("WARNING"):
            fonts = load_plt_fonts(catalog)
        assert fonts["Dup"].characters["A"].glyph == "PU0,0;"
        assert "declared by" in caplog.text

    def test_malformed_glyph_entry_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A character entry without a glyph string is skipped."""
        catalog = _write_catalog(
            tmp_path / "catalog",
            {"Test": _v2_font({"A": "PU0,0;", "B": {"bounding_box": {"min_x": 0}}})},
        )
        with caplog.at_level("WARNING"):
            fonts = load_plt_fonts(catalog)
        assert set(fonts["Test"].characters) == {"A"}
        assert "not a v2 glyph entry" in caplog.text

    def test_non_positive_normalized_height_falls_back(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A bogus normalized_ref_height warns and falls back to 1.0."""
        catalog = _write_catalog(
            tmp_path / "catalog", {"Test": _v2_font({"A": "PU0,0;"}, normalized_ref_height=0.0)}
        )
        with caplog.at_level("WARNING"):
            fonts = load_plt_fonts(catalog)
        assert fonts["Test"].normalized_ref_height == pytest.approx(1.0)
        assert "non-positive" in caplog.text

    def test_missing_directory_warns_and_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing catalog must not raise (TTF-only jobs keep working)."""
        with caplog.at_level("WARNING"):
            assert load_plt_fonts(tmp_path / "nope") == {}
        assert "not found" in caplog.text

    def test_malformed_json_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """An unparseable catalog file degrades to no PLT fonts."""
        catalog = tmp_path / "catalog"
        catalog.mkdir()
        (catalog / "bad.json").write_text("{not json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            assert load_plt_fonts(catalog) == {}
        assert "could not be read" in caplog.text

    def test_one_bad_file_does_not_sink_the_catalog(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A single unreadable font file skips only that font."""
        catalog = _write_catalog(tmp_path / "catalog", {"Good": _v2_font({"A": "x"})})
        (catalog / "bad.json").write_text("{not json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            fonts = load_plt_fonts(catalog)
        assert set(fonts) == {"Good"}
        assert "could not be read" in caplog.text

    def test_non_object_root_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """A JSON array file is rejected with a WARNING."""
        catalog = tmp_path / "catalog"
        catalog.mkdir()
        (catalog / "arr.json").write_text("[1, 2]", encoding="utf-8")
        with caplog.at_level("WARNING"):
            assert load_plt_fonts(catalog) == {}
        assert "not a JSON object" in caplog.text

    def test_non_map_font_entry_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A font entry that is not a v2 object is skipped."""
        catalog = tmp_path / "catalog"
        catalog.mkdir()
        (catalog / "Good.json").write_text(
            json.dumps({"font_name": "Good", **_v2_font({"A": "x"})}), encoding="utf-8"
        )
        (catalog / "Bad.json").write_text(
            json.dumps({"font_name": "Bad", "characters": [1, 2]}), encoding="utf-8"
        )
        with caplog.at_level("WARNING"):
            fonts = load_plt_fonts(catalog)
        assert set(fonts) == {"Good"}
        assert "Bad" in caplog.text

    def test_stroke_count_defaults_to_one(self, tmp_path: Path) -> None:
        """A catalog written before the multi-stroke convention loads as 1."""
        catalog = _write_catalog(tmp_path / "catalog", {"Test": _v2_font({"A": "PU0,0;"})})
        assert load_plt_fonts(catalog)["Test"].stroke_count == 1

    def test_stroke_count_parsed(self, tmp_path: Path) -> None:
        """The extractor's stroke_count metadata lands on the font data."""
        catalog = _write_catalog(
            tmp_path / "catalog", {"Test": _v2_font({"A": "PU0,0;"}, stroke_count=5)}
        )
        assert load_plt_fonts(catalog)["Test"].stroke_count == 5

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("5", 5), (5.0, 5), (0, 1), (-3, 1), ("five", 1), (True, 1), (None, 1)],
    )
    def test_stroke_count_lenient_parse(self, tmp_path: Path, raw: object, expected: int) -> None:
        """Garbage/stale stroke_count values fall back to single-stroke."""
        catalog = _write_catalog(
            tmp_path / "catalog", {"Test": _v2_font({"A": "PU0,0;"}, stroke_count=raw)}
        )
        assert load_plt_fonts(catalog)["Test"].stroke_count == expected

    def test_stroke_count_garbage_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A malformed stroke_count logs a WARNING and falls back to 1."""
        catalog = _write_catalog(
            tmp_path / "catalog", {"Test": _v2_font({"A": "PU0,0;"}, stroke_count="five")}
        )
        with caplog.at_level("WARNING"):
            fonts = load_plt_fonts(catalog)
        assert fonts["Test"].stroke_count == 1
        assert "stroke_count" in caplog.text


class TestPltFontStrokeCount:
    """The resolution-time stroke-count lookup."""

    @pytest.fixture
    def stroke_lib(self, tmp_path: Path) -> Path:
        """A catalog with a single-stroke and a five-stroke PLT font."""
        return _write_catalog(
            tmp_path / "catalog",
            {
                "Dino": _v2_font({"A": "PU0,0;"}),
                "Avenge5": _v2_font({"A": "PU0,0;"}, stroke_count=5),
            },
        )

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
        """TTF names are not PLT catalog keys -> single-stroke."""
        assert (
            font_registry.plt_font_stroke_count("Alpha", fonts_root / "VP-EL-Fonts" / "catalog")
            == 1
        )

    def test_blank_name_is_single_stroke(self, stroke_lib: Path) -> None:
        assert font_registry.plt_font_stroke_count("  ", stroke_lib) == 1

    def test_missing_library_is_single_stroke(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing catalog degrades (WARNING from the loader) to 1."""
        with caplog.at_level("WARNING"):
            assert font_registry.plt_font_stroke_count("Avenge5", tmp_path / "nope") == 1


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
        ref = resolve_font(
            "Dino", library_dir=fonts_root / "VP-EL-Fonts" / "catalog", fonts_dir=fonts_root
        )
        assert (ref.kind, ref.name) == ("plt", "Dino")
        assert ref.path is None

    @pytest.mark.parametrize("requested", ["dino", "DINO", "DiNo"])
    def test_case_insensitive(self, fonts_root: Path, requested: str) -> None:
        """Any casing resolves to the canonical name."""
        ref = resolve_font(
            requested, library_dir=fonts_root / "VP-EL-Fonts" / "catalog", fonts_dir=fonts_root
        )
        assert ref.name == "Dino"

    def test_surrounding_whitespace_tolerated(self, fonts_root: Path) -> None:
        """Padded names still resolve."""
        assert resolve_font("  Alpha  ", fonts_dir=fonts_root).name == "Alpha"

    def test_ttf_wins_cross_family_collision(self, tmp_path: Path) -> None:
        """A TTF basename shadowing a PLT key resolves to the TTF."""
        catalog = _write_catalog(tmp_path / "catalog", {"Dup": _v2_font({"A": "x"})})
        (tmp_path / "Dup.ttf").write_bytes(b"t")
        ref = resolve_font("dup", library_dir=catalog, fonts_dir=tmp_path)
        assert ref.kind == "ttf"

    def test_unknown_lists_choices(self, fonts_root: Path) -> None:
        """An unknown name raises with the full valid list."""
        with pytest.raises(FontNotFoundError) as excinfo:
            resolve_font(
                "Nope",
                library_dir=fonts_root / "VP-EL-Fonts" / "catalog",
                fonts_dir=fonts_root,
            )
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
                "jhanuni",
                library_dir=fonts_root / "VP-EL-Fonts" / "catalog",
                fonts_dir=fonts_root,
            )
            == "Jhanuni"
        )


class TestSmallCapsVariants:
    """Synthetic ``"<base> SC"`` small-caps variants."""

    def test_plt_variant_resolves_to_base_family(self, fonts_root: Path) -> None:
        """A PLT variant keeps the PLT kind, carries no path, and is flagged."""
        ref = resolve_font(
            "Dino SC", library_dir=fonts_root / "VP-EL-Fonts" / "catalog", fonts_dir=fonts_root
        )
        assert (ref.kind, ref.name, ref.path, ref.small_caps) == ("plt", "Dino SC", None, True)

    def test_ttf_variant_carries_base_path(self, fonts_root: Path) -> None:
        """A TTF variant resolves to the TTF family with the BASE file path."""
        ref = resolve_font("Alpha SC", fonts_dir=fonts_root)
        assert (ref.kind, ref.name, ref.small_caps) == ("ttf", "Alpha SC", True)
        assert ref.path == fonts_root / "Alpha.ttf"

    @pytest.mark.parametrize("requested", ["dino sc", "DINO SC", "DiNo Sc", " dino sc "])
    def test_variant_matching_is_case_insensitive(self, fonts_root: Path, requested: str) -> None:
        """Any casing/whitespace of the variant name resolves canonically."""
        ref = resolve_font(
            requested, library_dir=fonts_root / "VP-EL-Fonts" / "catalog", fonts_dir=fonts_root
        )
        assert ref.name == "Dino SC"
        assert ref.small_caps is True

    def test_base_font_is_not_flagged(self, fonts_root: Path) -> None:
        """A base name never reports small_caps."""
        ref = resolve_font(
            "Dino", library_dir=fonts_root / "VP-EL-Fonts" / "catalog", fonts_dir=fonts_root
        )
        assert ref.small_caps is False

    def test_choices_include_variants(self, fonts_root: Path) -> None:
        """Every base contributes a variant to the valid-name list."""
        choices = font_name_choices(
            library_dir=fonts_root / "VP-EL-Fonts" / "catalog", fonts_dir=fonts_root
        )
        assert {"Dino SC", "Jhanuni SC", "Alpha SC", "Beta SC"} <= set(choices)
        assert choices == sorted(choices, key=str.lower)

    def test_real_sc_named_font_wins_over_synthetic(self, tmp_path: Path) -> None:
        """A real TTF literally named ``Foo SC`` beats the synthetic variant."""
        (tmp_path / "Foo SC.ttf").write_bytes(b"t")
        catalog = _write_catalog(tmp_path / "catalog", {"Foo": _v2_font({"A": "x"})})
        ref = resolve_font("Foo SC", library_dir=catalog, fonts_dir=tmp_path)
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
        """The shipped non-exempt fonts each expose a working ``SC`` variant."""
        exempt = non_text_font_names()
        for base in font_name_choices():
            if base.lower().endswith(" sc") or base.lower() in exempt:
                continue
            ref = resolve_font(f"{base} SC")
            assert ref.small_caps is True
            assert ref.kind == resolve_font(base).kind
            assert ref.path == resolve_font(base).path

    def test_bundled_non_text_font_has_no_variant(self) -> None:
        """The shipped non-text list exempts Wingdings from its SC variant."""
        assert "Wingdings" in font_name_choices()
        assert "Wingdings SC" not in font_name_choices()
        with pytest.raises(FontNotFoundError):
            resolve_font("Wingdings SC")


class TestNonTextFonts:
    """The ``non_text_fonts.txt`` exemption list (symbol fonts, e.g. Wingdings)."""

    def _list_file(self, tmp_path: Path, body: str) -> Path:
        """Write a non-text font list file and return its path.

        Args:
            tmp_path: Directory to write into.
            body: Raw file contents.

        Returns:
            The written file's path.
        """
        path = tmp_path / "non_text_fonts.txt"
        path.write_text(body, encoding="utf-8")
        return path

    def test_bundled_file_lists_wingdings(self) -> None:
        """The shipped list exists and exempts Wingdings (case-insensitively)."""
        assert NON_TEXT_FONTS_FILE.is_file()
        assert "wingdings" in non_text_font_names()

    def test_missing_file_yields_no_exemptions(self, tmp_path: Path) -> None:
        """A missing list file is not an error - every SC variant survives."""
        catalog = _write_catalog(tmp_path / "catalog", {"Wingdings": _v2_font({"A": "x"})})
        assert non_text_font_names(tmp_path / "absent.txt") == frozenset()
        choices = font_name_choices(
            library_dir=catalog, fonts_dir=tmp_path, non_text_file=tmp_path / "absent.txt"
        )
        assert "Wingdings SC" in choices

    def test_comments_and_blank_lines_ignored(self, tmp_path: Path) -> None:
        """Full-line and trailing '#' comments plus blanks are skipped."""
        path = self._list_file(
            tmp_path,
            "# comment only\n\nWingdings\n  Dino  # trailing\n\n#end\n",
        )
        assert non_text_font_names(path) == frozenset({"wingdings", "dino"})

    def test_listed_font_loses_only_the_variant(self, tmp_path: Path) -> None:
        """A listed base stays selectable; only its SC variant disappears."""
        catalog = _write_catalog(
            tmp_path / "catalog",
            {"Wingdings": _v2_font({"A": "x"}), "Dino": _v2_font({"A": "x"})},
        )
        path = self._list_file(tmp_path, "Wingdings\n")
        choices = font_name_choices(library_dir=catalog, fonts_dir=tmp_path, non_text_file=path)
        assert "Wingdings" in choices
        assert "Wingdings SC" not in choices
        assert "Dino SC" in choices
        ref = resolve_font("Wingdings", library_dir=catalog, fonts_dir=tmp_path, non_text_file=path)
        assert (ref.kind, ref.name, ref.small_caps) == ("plt", "Wingdings", False)
        with pytest.raises(FontNotFoundError):
            resolve_font(
                "Wingdings SC", library_dir=catalog, fonts_dir=tmp_path, non_text_file=path
            )

    def test_real_sc_named_font_survives_its_base_being_listed(self, tmp_path: Path) -> None:
        """A real font literally named ``Foo SC`` keeps its name when its
        lookalike base ``Foo`` is listed (the exemption only skips the
        synthetic variant, never a real font)."""
        catalog = _write_catalog(tmp_path / "catalog", {"Foo": _v2_font({"A": "x"})})
        (tmp_path / "Foo SC.ttf").write_bytes(b"t")
        path = self._list_file(tmp_path, "Foo\n")
        choices = font_name_choices(library_dir=catalog, fonts_dir=tmp_path, non_text_file=path)
        assert "Foo SC" in choices
        ref = resolve_font("Foo SC", library_dir=catalog, fonts_dir=tmp_path, non_text_file=path)
        assert ref.kind == "ttf"
        assert ref.small_caps is False

    def test_normalize_font_name_threads_the_override(self, tmp_path: Path) -> None:
        """The schema-facing helper honours the exemption list too."""
        catalog = _write_catalog(tmp_path / "catalog", {"Wingdings": _v2_font({"A": "x"})})
        path = self._list_file(tmp_path, "Wingdings\n")
        assert (
            normalize_font_name(
                "Wingdings",
                library_dir=catalog,
                fonts_dir=tmp_path,
                non_text_file=path,
            )
            == "Wingdings"
        )
        with pytest.raises(FontNotFoundError):
            normalize_font_name(
                "Wingdings SC",
                library_dir=catalog,
                fonts_dir=tmp_path,
                non_text_file=path,
            )


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
        ref = resolve_font(
            "Dino", library_dir=fonts_root / "VP-EL-Fonts" / "catalog", fonts_dir=fonts_root
        )
        assert ref.italic_path is None
