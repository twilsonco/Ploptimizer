"""Tests for the font-showcase utility script (``scripts/font_showcase.py``).

The script is not part of the installed package, so it is loaded via
``importlib`` (mirroring ``tests/test_job_spec_docs.py``; its own
``sys.path`` bootstrap makes the ``plt_optimizer`` imports inside it work).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from plt_optimizer.generate.font_registry import font_name_choices
from plt_optimizer.generate.schema import JobSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "font_showcase.py"


def _load_script() -> Any:
    """Import ``scripts/font_showcase.py`` as a module by path.

    The module is registered in :data:`sys.modules` before execution: the
    mapping-mode :class:`~dataclasses.dataclass` resolves its annotations
    through ``sys.modules[cls.__module__]``, which fails for a module loaded
    by path and left unregistered.

    Returns:
        The loaded script module.
    """
    spec = importlib.util.spec_from_file_location("font_showcase", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["font_showcase"] = module
    spec.loader.exec_module(module)
    return module


script = _load_script()


class TestSafeFontFilename:
    """File-stem sanitization mirrors the generate CLI's job-id rules."""

    def test_plain_name_unchanged(self) -> None:
        """Canonical font names pass through untouched."""
        assert script.safe_font_filename("ReliefSingleLineCAD-Regular") == (
            "ReliefSingleLineCAD-Regular"
        )

    def test_whitespace_and_symbols(self) -> None:
        """Whitespace collapses to underscores; unsafe characters are stripped."""
        assert script.safe_font_filename("  My Font /v2!  ") == "My_Font_v2"

    def test_empty_falls_back(self) -> None:
        """A name sanitizing to nothing falls back to ``font``."""
        assert script.safe_font_filename("///") == "font"


class TestBuildShowcaseJob:
    """The per-font JobSpec template carries the seven-line sample block."""

    def test_font_name_is_first_line(self) -> None:
        """Line 0 is the font name itself, followed by the six sample lines."""
        job = script.build_showcase_job("Dino")
        assert isinstance(job, JobSpec)
        assert job.font == "Dino"
        assert job.content is not None
        assert [line.text for line in job.content] == [
            "Dino",
            *script.SHOWCASE_LINES,
        ]

    def test_compression_disabled(self) -> None:
        """max_h_compress is pinned to 0.0 so PLT arcs are never flattened."""
        job = script.build_showcase_job("Dino")
        assert job.max_h_compress == 0.0

    def test_width_override(self) -> None:
        """The widening re-render passes a custom label width through."""
        job = script.build_showcase_job("Dino", width=15.5)
        assert job.width == 15.5


class TestCoveredShowcaseLines:
    """The derived-glyph (Unicode) line follows the non-text font list."""

    def test_plt_font_keeps_the_unicode_line(self) -> None:
        """A letter font carries the derived glyphs, so the line stays."""
        lines = script._covered_showcase_lines("Dino")
        assert any(line.startswith("Unicode: ") for line in lines)

    def test_ttf_font_keeps_the_unicode_line(self) -> None:
        """TrueType fonts report full coverage (.notdef draws missing chars)."""
        lines = script._covered_showcase_lines("ReliefSingleLineCAD-Regular")
        assert any(line.startswith("Unicode: ") for line in lines)

    def test_non_text_font_drops_the_unicode_line(self) -> None:
        """Wingdings (shipped non-text list) engraves no Unicode line."""
        lines = script._covered_showcase_lines("Wingdings")
        assert not any(line.startswith("Unicode: ") for line in lines)
        # The remaining sample lines are untouched.
        assert lines == tuple(
            line for line in script.SHOWCASE_LINES if not line.startswith("Unicode: ")
        )

    def test_showcase_job_for_non_text_font_omits_the_line(self) -> None:
        """The built JobSpec content mirrors the pruned lines."""
        job = script.build_showcase_job("Wingdings")
        texts = [line.text for line in job.content] if job.content else []
        assert texts[0] == "Wingdings"
        assert not any(text.startswith("Unicode: ") for text in texts)


class TestMain:
    """End-to-end run writes one simple-outline PDF per available font."""

    @pytest.mark.parametrize("font", ["Dino", "ReliefSingleLineCAD-Regular"])
    def test_write_showcase_pdf(self, tmp_path: Path, font: str) -> None:
        """One PLT font and one TTF font each produce a non-empty PDF."""
        path = script.write_showcase_pdf(font, tmp_path)
        assert path == tmp_path / f"{font}.pdf"
        assert path.exists()
        assert path.read_bytes().startswith(b"%PDF")
        assert path.stat().st_size > 1000

    def test_main_writes_one_pdf_per_font(self, tmp_path: Path) -> None:
        """main() exits 0 and covers every font_name_choices() entry."""
        out = tmp_path / "showcase"
        rc = script.main(["--output", str(out)])
        assert rc == 0

        expected = {f"{script.safe_font_filename(f)}.pdf" for f in font_name_choices()}
        assert expected, "fixture fonts/ must contain at least one font"
        assert {p.name for p in out.glob("*.pdf")} == expected
        assert all(p.read_bytes().startswith(b"%PDF") for p in out.glob("*.pdf"))

    def test_main_survives_bad_font(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A font whose render raises is logged and skipped, others still write."""
        real_writer = script.write_showcase_pdf

        def flaky(font_name: str, output_dir: Path) -> Path:
            if font_name == font_name_choices()[0]:
                raise ValueError("simulated missing glyph")
            written: Path = real_writer(font_name, output_dir)
            return written

        monkeypatch.setattr(script, "write_showcase_pdf", flaky)
        out = tmp_path / "partial"
        rc = script.main(["--output", str(out)])
        assert rc == 0
        assert len(list(out.glob("*.pdf"))) == len(font_name_choices()) - 1


class TestLoadAsciiCharacters:
    """The mapping walks the shipped printable-ASCII list."""

    def test_shipped_file(self) -> None:
        """The shipped ``fonts/ascii.txt`` yields the 94 printable chars."""
        chars = script.load_ascii_characters()
        assert len(chars) == 94
        assert chars[0] == "!"
        assert chars[-1] == "~"
        assert all(len(char) == 1 for char in chars)
        assert not any(char.isspace() for char in chars)

    def test_custom_file_whitespace_runs_separate(self, tmp_path: Path) -> None:
        """Every whitespace run separates characters (extractor parity)."""
        path = tmp_path / "ascii.txt"
        path.write_text("a  b\tc\nd\n", encoding="utf-8")
        assert script.load_ascii_characters(path) == ["a", "b", "c", "d"]

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """An unreadable character file is a hard error."""
        with pytest.raises(ValueError, match="Failed to read"):
            script.load_ascii_characters(tmp_path / "absent.txt")

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        """A file carrying no characters is a hard error."""
        path = tmp_path / "ascii.txt"
        path.write_text("   \n\t\n", encoding="utf-8")
        with pytest.raises(ValueError, match="no characters"):
            script.load_ascii_characters(path)


class TestIsNonTextFont:
    """The non-text membership test is the shared, case-insensitive gate."""

    def test_bundled_symbol_font(self) -> None:
        """Wingdings (shipped non-text list) is a symbol font."""
        assert script.is_non_text_font("Wingdings")

    def test_case_insensitive(self) -> None:
        """Lookup matches the registry's lowercased list."""
        assert script.is_non_text_font("wingdings")
        assert script.is_non_text_font("WINGDINGS")

    def test_letter_font_is_not(self) -> None:
        """A letter font is not a symbol font."""
        assert not script.is_non_text_font("Dino")


class TestShowcaseTitles:
    """Letter fonts keep their title; symbol fonts name the mapping mode."""

    def test_letter_title_unchanged(self) -> None:
        """The letter-font title is the historical string."""
        assert script.showcase_pdf_title("Dino") == "Font showcase - Dino"

    def test_mapping_title_names_the_mode(self) -> None:
        """The symbol-font title says the page maps chars to symbols."""
        title = script.mapping_pdf_title("Wingdings")
        assert "Wingdings" in title
        assert "mapping" in title.lower()
        assert title.startswith("Font showcase - ")


class TestBuildMappingCells:
    """One key/symbol cell per ASCII character, keys always in the default face."""

    def test_one_cell_per_character(self) -> None:
        """Every requested character yields exactly one cell, in order."""
        chars = ["a", "b", "c"]
        cells = script.build_mapping_cells("Wingdings", chars)
        assert [cell.character for cell in cells] == chars

    def test_key_is_colon_label(self) -> None:
        """The key block carries the ``<char>:`` label (it has ink)."""
        cell = script.build_mapping_cells("Wingdings", ["a"])[0]
        assert not cell.key_block.is_empty()
        assert cell.key_width > 0.0

    def test_symbol_carries_glyph_width(self) -> None:
        """A covered slot renders a symbol block with positive width."""
        cell = script.build_mapping_cells("Wingdings", ["a"])[0]
        assert cell.symbol_block is not None
        assert not cell.symbol_block.is_empty()
        assert cell.symbol_width > 0.0
        assert cell.width >= cell.key_width + cell.symbol_width

    def test_missing_glyph_keeps_key_blank_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An ASCII slot the font lacks keeps its key with a blank symbol."""
        monkeypatch.setattr(script, "plt_font_has_glyph", lambda name, char: False)
        cell = script.build_mapping_cells("Wingdings", ["q"])[0]
        assert cell.symbol_block is None
        assert cell.symbol_width == 0.0
        assert not cell.key_block.is_empty()
        assert cell.width == cell.key_width + script.MAPPING_KEY_SYMBOL_GAP


class TestRenderMappingLayout:
    """The composed mapping grid is a parseable, framed HPGL document."""

    def test_framing_and_parseable(self) -> None:
        """The layout carries the pipeline framing and parses to segments."""
        from plt_optimizer.core.parser import PLTParser

        hpgl = script.render_mapping_layout("Wingdings", ["a", "b", "c"])
        assert hpgl.startswith(script.PLT_HEADER)
        assert hpgl.endswith(script.PLT_FOOTER)
        document = PLTParser().parse_string(hpgl)
        segments = sum(len(path.segments) for path in document.stroke_paths)
        assert segments > 0

    def test_empty_characters_raises(self) -> None:
        """A layout with no characters is a hard error."""
        with pytest.raises(ValueError, match="at least one character"):
            script.render_mapping_layout("Wingdings", [])

    def test_ttf_symbol_font_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A TrueType name on the non-text list renders through ftext."""
        ttf = "ReliefSingleLineCAD-Regular"
        monkeypatch.setattr(script, "non_text_font_names", lambda *a, **k: frozenset({ttf.lower()}))
        assert script.is_non_text_font(ttf)
        hpgl = script.render_mapping_layout(ttf, ["a", "b"])
        assert hpgl.startswith(script.PLT_HEADER)
        assert hpgl.endswith(script.PLT_FOOTER)


class TestMappingPdfDispatch:
    """Symbol fonts write a mapping PDF; letter fonts keep the sample block."""

    def test_write_showcase_pdf_dispatches_symbol_font(self, tmp_path: Path) -> None:
        """The symbol font's PDF is the mapping grid, named ``<font>.pdf``."""
        path = script.write_showcase_pdf("Wingdings", tmp_path)
        assert path == tmp_path / "Wingdings.pdf"
        assert path.exists()
        assert path.read_bytes().startswith(b"%PDF")
        assert path.stat().st_size > 1000

    def test_write_mapping_pdf_matches_dispatch(self, tmp_path: Path) -> None:
        """Direct and dispatched calls produce the same path."""
        direct = script.write_mapping_pdf("Wingdings", tmp_path)
        dispatched = script.write_showcase_pdf("Wingdings", tmp_path)
        assert direct == dispatched
