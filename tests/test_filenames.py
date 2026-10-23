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
)
from plt_optimizer.core.writer import PLTWriter


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
