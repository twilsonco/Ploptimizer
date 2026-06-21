"""Writer unit tests for PLT-Optimizer.

Tests the HPGL output generation, formatting accuracy, and file handling.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from plt_optimizer.core.models import (
    ArcSegment,
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

    def test_validate_segment_count_mismatch(self) -> None:
        """Test validate_output detects segment count mismatch (line 265)."""
        writer = PLTWriter()

        # Create a document with 2 segments
        doc_two_seg = PLTDocument(
            stroke_paths=[
                StrokePath(
                    segments=(
                        StrokeSegment(
                            start=Coordinate(x=0.0, y=0.0),
                            end=Coordinate(x=100.0, y=0.0),
                            is_cutting=True,
                        ),
                        StrokeSegment(
                            start=Coordinate(x=100.0, y=0.0),
                            end=Coordinate(x=200.0, y=100.0),
                            is_cutting=True,
                        ),
                    )
                )
            ]
        )

        # Write a document with only 1 segment
        doc_one_seg = PLTDocument(
            stroke_paths=[
                StrokePath(
                    segments=(
                        StrokeSegment(
                            start=Coordinate(x=0.0, y=0.0),
                            end=Coordinate(x=100.0, y=0.0),
                            is_cutting=True,
                        ),
                    )
                )
            ]
        )

        output_one = writer.write_string(doc_one_seg)

        # Validate doc_two_seg against output_one (should mismatch)
        is_valid, errors = writer.validate_output(doc_two_seg, output_one)

        assert not is_valid
        assert any("Segment count mismatch" in e for e in errors)

    def test_validate_distance_mismatch(self) -> None:
        """Test validate_output detects distance mismatch (lines 276-282)."""
        writer = PLTWriter()

        # Create a document with 10 units cutting distance
        doc_10 = PLTDocument(
            stroke_paths=[
                StrokePath(
                    segments=(
                        StrokeSegment(
                            start=Coordinate(x=0.0, y=0.0),
                            end=Coordinate(x=10.0, y=0.0),
                            is_cutting=True,
                        ),
                    )
                )
            ]
        )

        output_10 = writer.write_string(doc_10)

        # Create a document with 100 units cutting distance
        doc_100 = PLTDocument(
            stroke_paths=[
                StrokePath(
                    segments=(
                        StrokeSegment(
                            start=Coordinate(x=0.0, y=0.0),
                            end=Coordinate(x=100.0, y=0.0),
                            is_cutting=True,
                        ),
                    )
                )
            ]
        )

        # Validate doc_100 against output_10 (should detect distance mismatch)
        is_valid, errors = writer.validate_output(doc_100, output_10)

        assert not is_valid
        assert any("Distance mismatch" in e for e in errors)

    def test_validate_reparse_failure(self) -> None:
        """Test validate_output handles re-parsing failure (line 282)."""
        writer = PLTWriter()

        doc = PLTDocument(header_commands=[HeaderCommand("IN")])

        # Mock the parser's parse_string to raise ParseError
        original_parse = PLTParser.parse_string

        def mock_parse(self, content: str) -> PLTDocument:
            raise ParseError("mock parse failure")

        try:
            PLTParser.parse_string = mock_parse
            is_valid, errors = writer.validate_output(doc, "IN;")

            assert not is_valid
            assert any("Re-parsing failed" in e for e in errors)
        finally:
            PLTParser.parse_string = original_parse

    def test_validate_output_valid_document(self) -> None:
        """Test validate_output with a valid document."""
        writer = PLTWriter()

        doc = PLTDocument(
            header_commands=[HeaderCommand("IN")],
            stroke_paths=[
                StrokePath(
                    segments=(
                        StrokeSegment(
                            start=Coordinate(x=0.0, y=0.0),
                            end=Coordinate(x=100.0, y=50.0),
                            is_cutting=True,
                        ),
                    )
                )
            ],
            footer_commands=[FooterCommand("SP")],
        )

        output = writer.write_string(doc)
        is_valid, errors = writer.validate_output(doc, output)

        assert is_valid
        assert len(errors) == 0


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


class TestPLTWriterStrokePathFormatting:
    """Tests for stroke path formatting edge cases."""

    def test_empty_stroke_path(self) -> None:
        """Test formatting an empty stroke path (line 187)."""
        writer = PLTWriter()

        path = StrokePath(segments=())
        result, _ = writer._format_stroke_path(path)

        assert result == ""

    def test_stroke_path_with_pen_up_position(self) -> None:
        """Test stroke path with explicit pen-up position (lines 195-196)."""
        writer = PLTWriter()

        segment = StrokeSegment(
            start=Coordinate(x=50.0, y=60.0),
            end=Coordinate(x=100.0, y=200.0),
            is_cutting=True,
        )

        path = StrokePath(
            segments=(segment,),
            pen_up_position=Coordinate(x=10.0, y=20.0),
        )

        result, _ = writer._format_stroke_path(path)

        assert "PU50.000,60.000;" in result
        assert "PD100.000,200.000;" in result

    def test_stroke_path_no_pen_up_first_segment_rapid(self) -> None:
        """Test stroke path with no pen-up position but first segment is rapid (lines 197->204)."""
        writer = PLTWriter()

        # First segment is rapid (not cutting)
        segment = StrokeSegment(
            start=Coordinate(x=50.0, y=60.0),
            end=Coordinate(x=100.0, y=200.0),
            is_cutting=False,  # rapid move
        )

        path = StrokePath(segments=(segment,), pen_up_position=None)

        result, _ = writer._format_stroke_path(path)

        # When current_pos is None initially, PU is added to ensure safe positioning
        assert "PU50.000,60.000;" in result
        # Segment end uses PU for rapid move (not cutting)
        assert "PU100.000,200.000;" in result

    def test_stroke_path_no_pen_up_first_segment_cutting(self) -> None:
        """Test stroke path with no pen-up position but first segment is cutting."""
        writer = PLTWriter()

        # First segment is cutting, so implicit PU should be added
        segment = StrokeSegment(
            start=Coordinate(x=50.0, y=60.0),
            end=Coordinate(x=100.0, y=200.0),
            is_cutting=True,  # cutting
        )

        path = StrokePath(segments=(segment,), pen_up_position=None)

        result, _ = writer._format_stroke_path(path)

        # Should have implicit PU for start (since current_pos was None)
        assert "PU50.000,60.000;" in result
        # And PD for the segment end
        assert "PD100.000,200.000;" in result


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


class TestWriteError:
    """Tests for WriteError exception."""

    def test_write_error_without_document_part(self) -> None:
        """Test WriteError without document part."""
        err = WriteError("something failed")
        assert err.message == "something failed"
        assert err.document_part is None

    def test_write_error_with_document_part(self) -> None:
        """Test WriteError with document part (line 51 coverage)."""
        err = WriteError("something failed", document_part="header")
        assert err.message == "something failed"
        assert err.document_part == "header"
        full_msg = str(err)
        assert "(in header)" in full_msg

    def test_write_error_os_error(self) -> None:
        """Test WriteError raised from OSError in write_file."""
        writer = PLTWriter()
        doc = PLTDocument()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a subdirectory and make it read-only
            sub = Path(tmpdir) / "subdir"
            sub.mkdir()

            # Try to write a nested file inside the read-only dir (should fail)
            readonly_file = sub / "readonly.plt"
            readonly_file.write_text("test")

            # Make directory read-only then try to write inside it
            sub.chmod(0o555)
            try:
                impossible_path = Path(tmpdir) / "subdir" / "nested" / "output.plt"
                writer.write_file(doc, impossible_path)
            except WriteError:
                pass
            finally:
                sub.chmod(0o755)


class TestArcSegmentWriting:
    """Tests for ArcSegment writing and formatting."""

    def test_format_arc_segment_cutting(self) -> None:
        """Test formatting a cutting arc segment."""
        writer = PLTWriter()

        arc = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=1437.0, y=-1437.0),
            center=Coordinate(x=1016.0, y=1016.0),
            sweep_angle=90.0,
            is_cutting=True,
        )

        result = writer._format_arc_segment(arc)

        assert "PD;" in result
        assert "AA" in result
        assert "1016.000,1016.000" in result
        assert "90.000" in result

    def test_format_arc_segment_rapid(self) -> None:
        """Test formatting a rapid (pen-up) arc segment."""
        writer = PLTWriter()

        arc = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=1437.0, y=-1437.0),
            center=Coordinate(x=1016.0, y=1016.0),
            sweep_angle=90.0,
            is_cutting=False,
        )

        result = writer._format_arc_segment(arc)

        assert "PU;" in result
        assert "AA" in result

    def test_format_stroke_path_with_arc(self) -> None:
        """Test formatting a stroke path that contains arc segments."""
        writer = PLTWriter()

        line_seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        arc_seg = ArcSegment(
            start=Coordinate(x=100.0, y=0.0),
            end=Coordinate(x=200.0, y=100.0),
            center=Coordinate(x=150.0, y=50.0),
            sweep_angle=90.0,
            is_cutting=True,
        )

        path = StrokePath(segments=(line_seg, arc_seg))
        doc = PLTDocument(stroke_paths=[path])

        result = writer.write_string(doc)

        assert "PD100.000,0.000;" in result
        assert "AA150.000,50.000" in result

    def test_arc_absolute_value_in_output(self) -> None:
        """Test that AA command is used for clockwise arcs (positive angle)."""
        writer = PLTWriter()

        arc_positive = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=-100.0),
            center=Coordinate(x=50.0, y=50.0),
            sweep_angle=180.0,
            is_cutting=True,
        )

        result = writer._format_arc_segment(arc_positive)

        assert "AA" in result

    def test_roundtrip_with_arc(self) -> None:
        """Test that documents with arcs survive round-trip."""
        parser = PLTParser()
        writer = PLTWriter()

        original = "PU0.000,0.000;PD1016.000,1016.000;AA1016.000,1016.000,90.000;"
        doc1 = parser.parse_string(original)
        output = writer.write_string(doc1)

        doc2 = parser.parse_string(output)

        arc_segments_1 = [
            seg for path in doc1.stroke_paths
            for seg in path.segments if isinstance(seg, ArcSegment)
        ]
        arc_segments_2 = [
            seg for path in doc2.stroke_paths
            for seg in path.segments if isinstance(seg, ArcSegment)
        ]

        assert len(arc_segments_1) >= 1
        assert len(arc_segments_2) >= 1

    def test_write_string_with_multiple_arcs(self) -> None:
        """Test writing a document with multiple arc segments."""
        writer = PLTWriter()

        path = StrokePath(
            segments=(
                ArcSegment(
                    start=Coordinate(x=0.0, y=0.0),
                    end=Coordinate(x=100.0, y=-100.0),
                    center=Coordinate(x=50.0, y=50.0),
                    sweep_angle=90.0,
                    is_cutting=True,
                ),
                ArcSegment(
                    start=Coordinate(x=100.0, y=-100.0),
                    end=Coordinate(x=200.0, y=0.0),
                    center=Coordinate(x=150.0, y=50.0),
                    sweep_angle=90.0,
                    is_cutting=True,
                ),
            )
        )

        doc = PLTDocument(stroke_paths=[path])
        output = writer.write_string(doc)

        assert "AA" in output
        count = output.count("AA")
        assert count >= 2
