"""Tests for the font-showcase utility script (``scripts/font_showcase.py``).

The script is not part of the installed package, so it is loaded via
``importlib`` (mirroring ``tests/test_job_spec_docs.py``; its own
``sys.path`` bootstrap makes the ``plt_optimizer`` imports inside it work).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from plt_optimizer.generate.font_registry import font_name_choices
from plt_optimizer.generate.schema import JobSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "font_showcase.py"


def _load_script() -> Any:
    """Import ``scripts/font_showcase.py`` as a module by path.

    Returns:
        The loaded script module.
    """
    spec = importlib.util.spec_from_file_location("font_showcase", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
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
    """The per-font JobSpec template carries the five-line sample block."""

    def test_font_name_is_first_line(self) -> None:
        """Line 0 is the font name itself, followed by the four sample lines."""
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
        assert expected, "fixture Fonts/ must contain at least one font"
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
