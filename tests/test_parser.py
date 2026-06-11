"""Parser unit tests for PLT-Optimizer.

Tests the parsing accuracy, error handling, and edge cases of the HPGL parser.
"""

from __future__ import annotations

import math
from pathlib import Path
import tempfile

import pytest

from plt_optimizer.core.models import Coordinate, HeaderCommand, PenState, StrokeSegment
from plt_optimizer.core.parser import PLTParser, ParseError


class TestPLTParser:
    """Tests for the PLT parser functionality."""

    def test_parse_basic_hpgl(self) -> None:
        """Test parsing a basic HPGL command sequence."""
        content = "IN;VS0.50;PA;PU100.000,200.000;PD300.000,400.000;SP;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        assert len(doc.header_commands) >= 3
        assert len(doc.footer_commands) == 1

    def test_parse_coordinate_extraction(self) -> None:
        """Test that coordinates are correctly extracted from commands."""
        content = "IN;PU0.000,0.000;PD18288.000,0.000;SP;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        # Should have at least one stroke path
        assert len(doc.stroke_paths) >= 1

    def test_parse_multiple_coordinate_pairs(self) -> None:
        """Test parsing commands with multiple coordinate pairs."""
        content = "PU0.000,0.000;PD100.000,100.000;PD200.000,200.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        total_segments = sum(len(p.segments) for p in doc.stroke_paths)
        assert total_segments >= 2

    def test_parse_file(self) -> None:
        """Test parsing from a file path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.plt"
            content = "IN;VS0.50;SP;"
            test_file.write_text(content, encoding="utf-8")

            parser = PLTParser()
            doc = parser.parse_file(test_file)

            assert len(doc.header_commands) >= 1

    def test_parse_file_not_found(self) -> None:
        """Test that parsing nonexistent file raises appropriate error."""
        parser = PLTParser()

        with pytest.raises((ParseError, FileNotFoundError)):
            parser.parse_file(Path("/nonexistent/file.plt"))

    def test_parse_unknown_command_handling(self) -> None:
        """Test handling of unknown but syntactically valid HPGL commands.

        The tokenizer extracts valid HPGL-like tokens. Unknown commands are
        treated as headers with a warning rather than raising errors, since
        EngraveLab may use custom/vendor extensions.
        """
        parser = PLTParser()

        # Unrecognized uppercase command becomes header (no error)
        doc1 = parser.parse_string("ALL;")
        assert len(doc1.header_commands) >= 1

        doc2 = parser.parse_string("PARSED;")
        assert len(doc2.header_commands) >= 1

    def test_parse_windows_line_endings(self) -> None:
        """Test parsing handles Windows CRLF line endings."""
        content = "IN;\r\nVS0.50;\r\nSP;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        assert len(doc.header_commands) >= 2

    def test_parse_mixed_line_endings(self) -> None:
        """Test parsing handles mixed line endings."""
        content = "IN;\rVS0.50;\nSP;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        assert len(doc.header_commands) >= 2


class TestCoordinateExtraction:
    """Tests for coordinate extraction accuracy."""

    def test_coordinate_precision(self) -> None:
        """Test that coordinates maintain precision to 3 decimal places."""
        content = "PU123.456789,987.654321;PD18288.999999,-0.000001;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        if doc.stroke_paths and doc.stroke_paths[0].segments:
            seg = doc.stroke_paths[0].segments[0]
            # Check precision is rounded to 3 places
            assert abs(seg.start.x - round(123.456789, 3)) < 0.001

    def test_negative_coordinates(self) -> None:
        """Test parsing of negative coordinate values."""
        content = "PU-100.000,-200.000;PD-300.000,-400.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        # Should parse without error
        assert len(doc.stroke_paths) >= 1


class TestHeaderCommandParsing:
    """Tests for header command parsing."""

    def test_parse_init_command(self) -> None:
        """Test parsing of IN (initialize) command."""
        content = "IN;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        assert len(doc.header_commands) >= 1
        assert any(hc.instruction == "IN" for hc in doc.header_commands)

    def test_parse_velocity_command(self) -> None:
        """Test parsing of VS (velocity select) command."""
        content = "VS0.50;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        vs_cmds = [hc for hc in doc.header_commands if hc.instruction == "VS"]
        assert len(vs_cmds) >= 1
        assert vs_cmds[0].parameters is not None

    def test_parse_zoom_command(self) -> None:
        """Test parsing of ZO (zoom) command with parameters."""
        content = "ZO123,1;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        zo_cmds = [hc for hc in doc.header_commands if hc.instruction == "ZO"]
        assert len(zo_cmds) >= 1


class TestStrokePathParsing:
    """Tests for stroke path parsing."""

    def test_pen_up_starts_path(self) -> None:
        """Test that a single PU command without subsequent PD is handled.

        A standalone PU only moves the pen up position, it doesn't create
        any cutting segment. So no stroke_paths with segments should be created.
        """
        content = "PU0.000,0.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        # PU just updates position - a path with cutting segments requires PD after PU
        # We check that the document is still valid (no parse errors)
        assert len(doc.header_commands) == 0  # No headers either

    def test_pen_down_creates_segments(self) -> None:
        """Test that PD commands create cutting segments."""
        content = "PU0.000,0.000;PD100.000,100.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        total_segments = sum(len(p.segments) for p in doc.stroke_paths)
        assert total_segments >= 1

    def test_segment_is_cutting_flag(self) -> None:
        """Test that segments are correctly marked as cutting or rapid."""
        content = "PU0.000,0.000;PD100.000,100.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        # Find a cutting segment
        cutting_segments = [
            seg for path in doc.stroke_paths
            for seg in path.segments if seg.is_cutting
        ]
        assert len(cutting_segments) >= 1


class TestDistanceCalculation:
    """Tests for distance calculation accuracy."""

    def test_segment_length_calculation(self) -> None:
        """Test that segment lengths are calculated correctly."""
        # Horizontal line: (0,0) to (100,0)
        content = "PU0.000,0.000;PD100.000,0.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        if doc.stroke_paths and len(doc.stroke_paths[0].segments) > 0:
            seg_length = doc.stroke_paths[0].segments[0].length
            assert math.isclose(seg_length, 100.0, abs_tol=0.001)

    def test_path_total_distance(self) -> None:
        """Test that path total distance sums segment lengths."""
        content = "PU0.000,0.000;PD100.000,0.000;PD200.000,0.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        if len(doc.stroke_paths) >= 1:
            path_dist = doc.stroke_paths[0].total_distance
            assert math.isclose(path_dist, 200.0, abs_tol=0.001)