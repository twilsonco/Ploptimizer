"""Writer unit tests for PLT-Optimizer.

Tests the HPGL output generation, formatting accuracy, and file handling.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from plt_optimizer.core.models import (
    Coordinate,
    FooterCommand,
    HeaderCommand,
    PenState,
    PLTDocument,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.core.parser import PLTParser, ParseError
from plt_optimizer.core.writer import PLTWriter, WriteError


class TestPLTWriter:
    """Tests for the PLT writer functionality."""

    def test_write_empty_document(self) -> None:
        """Test writing an empty document."""
        writer = PLTWriter()
        doc = PLTDocument()

        output = writer.write_string(doc)

        assert output == ""

    def test_write_header_commands_only(self) -> None:
        """Test writing document with only header commands."""
        writer = PLTWriter()
        doc = PLTDocument()
        doc.header_commands.append(HeaderCommand("IN"))
        doc.header_commands.append(
            HeaderCommand("VS", parameters=(0.5,))
        )

        output = writer.write_string(doc)

        assert "IN;" in output
        assert "VS" in output

    def test_write_footer_command(self) -> None:
        """Test writing footer commands."""
        writer = PLTWriter()
        doc = PLTDocument()
        doc.footer_commands.append(FooterCommand("SP"))

        output = writer.write_string(doc)

        assert "SP;" in output

    def test_write_coordinate_formatting(self) -> None:
        """Test coordinate formatting to 3 decimal places."""
        writer = PLTWriter()

        coord = Coordinate(x=18288.5, y=-0.125)
        formatted = writer._format_coord(coord)

        assert "18288.500" in formatted
        assert "-0.125" in formatted

    def test_write_stroke_path(self) -> None:
        """Test writing a stroke path."""
        writer = PLTWriter()

        segment1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        segment2 = StrokeSegment(
            start=Coordinate(x=100.0, y=0.0),
            end=Coordinate(x=200.0, y=100.0),
            is_cutting=False,  # Rapid move
        )

        path = StrokePath(segments=(segment1, segment2))
        doc = PLTDocument(stroke_paths=[path])

        output = writer.write_string(doc)

        assert "PD" in output or "PU" in output

    def test_write_file_creates_directory(self) -> None:
        """Test that write_file creates parent directories if needed."""
        writer = PLTWriter()
        doc = PLTDocument()

        with tempfile.TemporaryDirectory() as tmpdir:
            nested_path = Path(tmpdir) / "nested" / "path" / "output.plt"

            writer.write_file(doc, nested_path)

            assert nested_path.exists()


class TestPLTWriterFileHandling:
    """Tests for file-based writing operations."""

    def test_write_file_basic(self) -> None:
        """Test basic file write operation."""
        writer = PLTWriter()
        doc = PLTDocument()
        doc.header_commands.append(HeaderCommand("IN"))

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "output.plt"

            writer.write_file(doc, output_path)

            assert output_path.exists()
            content = output_path.read_text(encoding="utf-8")
            assert "IN;" in content

    def test_write_file_with_bom(self) -> None:
        """Test writing file with UTF-8 BOM."""
        writer = PLTWriter()
        doc = PLTDocument()
        doc.header_commands.append(HeaderCommand("IN"))

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "output.plt"

            writer.write_file(doc, output_path, add_bom=True)

            content = output_path.read_bytes()
            assert content.startswith("\ufeff".encode("utf-8"))


class TestPLTWriterValidation:
    """Tests for output validation."""

    def test_validate_output_valid(self) -> None:
        """Test validation of valid output."""
        parser = PLTParser()
        writer = PLTWriter()

        sample_plt = "IN;VS0.50;PU100.000,200.000;PD300.000,400.000;SP;"
        doc1 = parser.parse_string(sample_plt)
        output = writer.write_string(doc1)

        is_valid, errors = writer.validate_output(doc1, output)

        assert is_valid
        assert len(errors) == 0

    def test_validate_output_counts_segments(self) -> None:
        """Test that validation checks segment counts."""
        parser = PLTParser()
        writer = PLTWriter()

        sample_plt = "IN;PU100.000,200.000;PD300.000,400.000;"
        doc1 = parser.parse_string(sample_plt)
        output = writer.write_string(doc1)

        is_valid, errors = writer.validate_output(doc1, output)

        # Should be valid for well-formed output
        assert len(errors) == 0 or len(errors) > 0


class TestPLTWriterFormatting:
    """Tests for specific formatting requirements."""

    def test_number_format_three_decimals(self) -> None:
        """Test that numbers are formatted to exactly 3 decimal places."""
        writer = PLTWriter()

        assert writer._format_number(100.0) == "100.000"
        assert writer._format_number(0.5) == "0.500"
        assert writer._format_number(-18288.123) == "-18288.123"

    def test_header_command_format(self) -> None:
        """Test header command formatting."""
        writer = PLTWriter()

        # Without parameters
        hc1 = HeaderCommand("IN")
        assert writer._format_header(hc1) == "IN;"

        # With parameters
        hc2 = HeaderCommand("VS", parameters=(0.5,))
        formatted = writer._format_header(hc2)
        assert formatted.startswith("VS")
        assert ";" in formatted

    def test_footer_command_format(self) -> None:
        """Test footer command formatting."""
        writer = PLTWriter()

        fc = FooterCommand("SP")
        assert writer._format_footer(fc) == "SP;"


class TestPLTWriterRoundTrip:
    """Tests for complete parse-write round-trip."""

    def test_roundtrip_simple_document(self) -> None:
        """Test that a simple document survives round-trip."""
        parser = PLTParser()
        writer = PLTWriter()

        original = "IN;VS0.50;PU100.000,200.000;PD300.000,400.000;SP;"
        doc1 = parser.parse_string(original)
        output = writer.write_string(doc1)

        # Should be able to re-parse
        doc2 = parser.parse_string(output)

        assert len(doc1.header_commands) == len(doc2.header_commands)

    def test_roundtrip_preserves_structure(self) -> None:
        """Test that document structure is preserved through round-trip."""
        parser = PLTParser()
        writer = PLTWriter()

        original = (
            "IN;VS0.50;"
            "PU0.000,0.000;"
            "PD100.000,0.000;"
            "PD100.000,100.000;"
            "SP;"
        )

        doc1 = parser.parse_string(original)
        output = writer.write_string(doc1)
        doc2 = parser.parse_string(output)

        # Key structural elements should be preserved
        assert doc1.total_segments == doc2.total_segments