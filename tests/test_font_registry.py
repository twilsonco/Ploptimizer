"""Tests for font discovery and name resolution (``font_registry``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plt_optimizer.generate import font_registry
from plt_optimizer.generate.font_registry import (
    DEFAULT_FONT_NAME,
    FONTS_DIR,
    PLT_FONTS_JSON_PATH,
    FontNotFoundError,
    available_ttf_fonts,
    font_name_choices,
    load_plt_fonts,
    normalize_font_name,
    resolve_font,
)


@pytest.fixture(autouse=True)
def _clear_caches() -> object:
    """Isolate the module's lru_caches between tests."""
    load_plt_fonts.cache_clear()
    available_ttf_fonts.cache_clear()
    yield
    load_plt_fonts.cache_clear()
    available_ttf_fonts.cache_clear()


@pytest.fixture
def fonts_root(tmp_path: Path) -> Path:
    """A synthetic Fonts root with one PLT library and two TTFs."""
    (tmp_path / "Sub").mkdir()
    (tmp_path / "Alpha.ttf").write_bytes(b"ttf-a")
    (tmp_path / "Sub" / "Beta.ttf").write_bytes(b"ttf-b")
    (tmp_path / "ignored.otf").write_bytes(b"otf")
    library = tmp_path / "plt_fonts.json"
    library.write_text(
        json.dumps({"Dino": {"A": "PU0,0;PD1,1;"}, "Jhanuni": {"A": "PU0,0;"}}),
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
        """The shipped plt_fonts.json parses into glyph maps."""
        fonts = load_plt_fonts()
        assert fonts, "bundled plt_fonts.json produced no fonts"
        for name, glyphs in fonts.items():
            assert name
            assert all(isinstance(char, str) and isinstance(cmd, str) for char, cmd in glyphs.items())

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
        """A well-formed library yields name -> character map."""
        fonts = load_plt_fonts(fonts_root / "plt_fonts.json")
        assert set(fonts) == {"Dino", "Jhanuni"}
        assert fonts["Dino"]["A"] == "PU0,0;PD1,1;"

    def test_missing_file_warns_and_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing library must not raise (TTF-only jobs keep working)."""
        with caplog.at_level("WARNING"):
            assert load_plt_fonts(tmp_path / "nope.json") == {}
        assert "not found" in caplog.text

    def test_malformed_json_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
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
        """A font entry that is not a character map is skipped."""
        bad = tmp_path / "plt_fonts.json"
        bad.write_text(json.dumps({"Good": {"A": "x"}, "Bad": [1, 2]}), encoding="utf-8")
        with caplog.at_level("WARNING"):
            fonts = load_plt_fonts(bad)
        assert set(fonts) == {"Good"}
        assert "Bad" in caplog.text


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
        ref = resolve_font(
            requested, json_path=fonts_root / "plt_fonts.json", fonts_dir=fonts_root
        )
        assert ref.name == "Dino"

    def test_surrounding_whitespace_tolerated(self, fonts_root: Path) -> None:
        """Padded names still resolve."""
        assert resolve_font("  Alpha  ", fonts_dir=fonts_root).name == "Alpha"

    def test_ttf_wins_cross_family_collision(self, tmp_path: Path) -> None:
        """A TTF basename shadowing a PLT key resolves to the TTF."""
        library = tmp_path / "plt_fonts.json"
        library.write_text(json.dumps({"Dup": {"A": "x"}}), encoding="utf-8")
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
