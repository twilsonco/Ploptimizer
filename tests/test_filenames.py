"""Tests for the shared plotter-compatible filename helpers.

``plt_optimizer/core/filenames.py`` owns the 39-character controller limit
used by BOTH pipelines: the optimize/watch writer (via
``PLTWriter._ensure_filename_length``) and the generate export
(``export_per_cutter_plts`` PLT write sites).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from plt_optimizer.core.filenames import (
    MAX_PLOT_FILENAME_CHARS,
    ensure_plot_filename_length,
    sanitize_filename_component,
)
from plt_optimizer.core.writer import PLTWriter


class TestSanitizeFilenameComponent:
    """Unit tests for sanitize_filename_component."""

    def test_punctuation_becomes_underscore(self) -> None:
        """Every punctuation run collapses to a single underscore."""
        assert sanitize_filename_component("a/b\\c:d*e") == "a_b_c_d_e"
        assert sanitize_filename_component("wb(uv)") == "wb_uv"
        assert sanitize_filename_component("2026-10-06 seer wb-uv") == "2026_10_06_seer_wb_uv"
        assert sanitize_filename_component("Sign!!#3") == "Sign_3"

    def test_underscores_preserved(self) -> None:
        """Underscores are the one allowed punctuation mark."""
        assert sanitize_filename_component("keep_underscores_9") == "keep_underscores_9"
        assert sanitize_filename_component("keep.dots-and_9") == "keep_dots_and_9"

    def test_whitespace_runs_collapse(self) -> None:
        """Whitespace is punctuation, so runs become one underscore."""
        assert sanitize_filename_component("  Hello   World!  ") == "Hello_World"

    def test_international_characters_transliterate(self) -> None:
        """Accented Latin letters reduce to their ASCII base."""
        assert sanitize_filename_component("Café Sign #3 (2x4)") == "Cafe_Sign_3_2x4"
        assert sanitize_filename_component("Ñoño") == "Nono"
        assert sanitize_filename_component("naïve résumé") == "naive_resume"
        # Compatibility forms (ligatures, fullwidth) decompose under NFKD.
        assert sanitize_filename_component("ﬁle") == "file"
        assert sanitize_filename_component("Ｆｕｌｌ") == "Full"

    def test_no_ascii_form_falls_back(self) -> None:
        """Scripts with no ASCII transliteration use the fallback."""
        assert sanitize_filename_component("日本語") == "job"
        assert sanitize_filename_component("日本語", fallback="file") == "file"
        assert sanitize_filename_component("") == "job"
        assert sanitize_filename_component("!!!") == "job"
        # Partial coverage keeps the surviving ASCII part, not the fallback.
        assert sanitize_filename_component("Ñoño 日本語") == "Nono"

    def test_result_is_safe_charset(self) -> None:
        """The output never contains punctuation outside the underscore."""
        raw = "a!b@c#d$e%f^g&h*i(j)k+l=m.n,o/p;:<>?\\|~`'\"{}[]()_9 \t"
        result = sanitize_filename_component(raw)
        assert set(result) <= set("abcdefghijklmnopqrstuvwxyz0123456789_")
        assert "__" not in result
        assert not result.startswith("_")
        assert not result.endswith("_")

    def test_no_leading_or_trailing_underscore(self) -> None:
        """Separators at the edges are stripped."""
        assert sanitize_filename_component("__weird__") == "weird"
        assert sanitize_filename_component("...dots...") == "dots"

    def test_windows_reserved_names_guarded(self) -> None:
        """Reserved device names gain a trailing underscore."""
        assert sanitize_filename_component("con") == "con_"
        assert sanitize_filename_component("CON") == "CON_"
        assert sanitize_filename_component("nul") == "nul_"
        assert sanitize_filename_component("com1") == "com1_"
        assert sanitize_filename_component("lpt9") == "lpt9_"
        # Not reserved: substring matches and out-of-range numbers.
        assert sanitize_filename_component("cone") == "cone"
        assert sanitize_filename_component("com10") == "com10"
        # The fallback is guarded too.
        assert sanitize_filename_component("日本語", fallback="nul") == "nul_"


class TestEnsurePlotFilenameLength:
    """Unit tests for ensure_plot_filename_length."""

    def test_short_name_unchanged(self) -> None:
        """A name within the limit is returned as-is."""
        path = Path("/out/60_txt_job.plt")
        assert ensure_plot_filename_length(path) == path

    def test_exact_limit_unchanged(self) -> None:
        """A name of exactly MAX characters is NOT shortened (boundary)."""
        name = "b" * (MAX_PLOT_FILENAME_CHARS - 4) + ".plt"
        path = Path("/out") / name
        assert len(path.name) == MAX_PLOT_FILENAME_CHARS
        assert ensure_plot_filename_length(path) == path

    def test_long_name_truncated_to_limit(self) -> None:
        """A longer name is cut from the right, extension preserved."""
        path = Path("/out") / ("a" * 50 + ".plt")
        result = ensure_plot_filename_length(path)
        assert len(result.name) == MAX_PLOT_FILENAME_CHARS
        assert result.suffix == ".plt"
        assert result.name == "a" * 35 + ".plt"
        assert result.parent == path.parent

    def test_directory_part_untouched(self) -> None:
        """Only the filename is capped; deep/long directories stay."""
        path = Path("/some/very/deep/directory/that/is/long") / ("x" * 60 + ".plt")
        result = ensure_plot_filename_length(path)
        assert result.parent == path.parent

    def test_warning_logged_when_shortened(self, caplog: pytest.LogCaptureFixture) -> None:
        """The shortening is reported at WARNING with both names."""
        logger = logging.getLogger("test.filenames")
        path = Path("/out") / ("c" * 44 + ".plt")
        with caplog.at_level(logging.WARNING, logger="test.filenames"):
            result = ensure_plot_filename_length(path, logger)
        assert len(result.name) == MAX_PLOT_FILENAME_CHARS
        messages = [record.message for record in caplog.records]
        assert any(path.name in message and result.name in message for message in messages)

    def test_no_warning_when_unchanged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Names within the limit log nothing."""
        logger = logging.getLogger("test.filenames.quiet")
        with caplog.at_level(logging.WARNING, logger="test.filenames.quiet"):
            ensure_plot_filename_length(Path("/out/short.plt"), logger)
        assert caplog.records == []

    def test_custom_max_chars(self) -> None:
        """The budget is overridable (e.g. 8.3-style shorter limits)."""
        path = Path("/out/abcdefghijkl.plt")  # 16 chars, over the 14 budget
        result = ensure_plot_filename_length(path, max_chars=14)
        assert result.name == "abcdefghij.plt"
        assert len(result.name) == 14

    def test_truncation_onto_reserved_name_is_de_reserved(self) -> None:
        """A stem cut exactly onto a Windows device name has its last
        character replaced (keeps the budget, un-reserves the name)."""
        path = Path("/out") / ("com1" + "9" * 40 + ".plt")
        # Budget 8 -> 4 stem chars -> "com1", which Windows would reserve.
        result = ensure_plot_filename_length(path, max_chars=8)
        assert result.name == "com_.plt"
        assert len(result.name) == 8
        assert result.stem.upper() not in {"COM1", "CON", "NUL", "LPT1"}

    def test_truncation_preserving_length(self) -> None:
        """The default-cap truncation stays exactly at the limit."""
        path = Path("/out") / ("com1_" + "9" * 40 + ".plt")
        result = ensure_plot_filename_length(path)
        assert len(result.name) == MAX_PLOT_FILENAME_CHARS
        assert result.name == "com1_" + "9" * 30 + ".plt"

    def test_short_budget_onto_reserved_name(self) -> None:
        """The guard also fires for a shorter custom budget."""
        result = ensure_plot_filename_length(Path("/out/com1_abcdefgh.plt"), max_chars=8)
        assert result.name == "com_.plt"

    def test_extension_fills_budget_left_unchanged(self, caplog: pytest.LogCaptureFixture) -> None:
        """A name that cannot be shortened (no stem budget) is kept, with a warning."""
        logger = logging.getLogger("test.filenames.suffix")
        path = Path("/out/ab.tar.gz")
        with caplog.at_level(logging.WARNING, logger="test.filenames.suffix"):
            result = ensure_plot_filename_length(path, logger, max_chars=3)
        assert result == path
        assert any("no room" in record.message for record in caplog.records)


class TestPLTWriterDelegation:
    """PLTWriter._ensure_filename_length delegates to the shared helper."""

    def test_writer_shortens_long_filenames(self) -> None:
        """The watch-side method keeps its 39-char contract."""
        writer = PLTWriter()
        path = Path("/out") / ("a" * 50 + ".plt")
        new_path = writer._ensure_filename_length(path)
        assert len(new_path.name) == MAX_PLOT_FILENAME_CHARS
        assert new_path.suffix == ".plt"

    def test_writer_keeps_short_filenames(self) -> None:
        """Short names pass through unchanged."""
        writer = PLTWriter()
        path = Path("/out/short.plt")
        assert writer._ensure_filename_length(path) == path
