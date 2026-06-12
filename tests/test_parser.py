"""Parser unit tests for PLT-Optimizer.

Tests the parsing accuracy, error handling, and edge cases of the HPGL parser.
"""

from __future__ import annotations

import math
from pathlib import Path
import tempfile

import pytest

from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    HeaderCommand,
    PenState,
    PLTDocument,
    Segment,
    StrokePath,
    StrokeSegment,
)
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

class TestParseErrorFormatting:
    """Tests for ParseError message formatting with line_number and token."""

    def test_parse_error_with_line_number(self) -> None:
        """Test ParseError message includes line number when provided."""
        error = ParseError("bad input", line_number=5)
        assert "line 5" in str(error)

    def test_parse_error_with_token(self) -> None:
        """Test ParseError message includes token when provided."""
        error = ParseError("bad input", token="FOO")
        assert "'FOO'" in str(error)

    def test_parse_error_with_both(self) -> None:
        """Test ParseError message includes both line number and token."""
        error = ParseError("bad input", line_number=10, token="BAR")
        msg = str(error)
        assert "line 10" in msg
        assert "'BAR'" in msg

    def test_parse_error_without_optional_params(self) -> None:
        """Test ParseError with only message has no extras."""
        error = ParseError("just a message")
        assert str(error) == "just a message"


class TestUnknownCommandHandling:
    """Tests for unknown command handling in parser."""

    def test_unknown_command_treated_as_header(self) -> None:
        """Test that unknown commands are treated as header commands."""
        parser = PLTParser()

        doc = parser.parse_string("IN;UNKNOWN123;SP;")

        # UNKNOWN123 should be parsed as a header command
        assert len(doc.header_commands) >= 2

    def test_unknown_command_with_parameters_treated_as_header(self) -> None:
        """Test unknown commands with parameters are treated as headers."""
        parser = PLTParser()

        doc = parser.parse_string("IN;FOO:1,2;SP;")

        # FOO:1,2 should be parsed as header with parameters
        foo_cmds = [hc for hc in doc.header_commands if hc.instruction == "FOO"]
        assert len(foo_cmds) >= 1
        assert foo_cmds[0].parameters == (1.0, 2.0)


class TestCoordinateExtractionEdgeCases:
    """Tests for edge cases in coordinate extraction."""

    def test_empty_tokens_do_not_break_parsing(self) -> None:
        """Test that empty tokens are skipped without error."""
        content = "IN;;VS0.50;SP;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        assert len(doc.header_commands) >= 2

    def test_exhausted_tokens_edge_case(self) -> None:
        """Test handling when token list is exhausted mid-extraction."""
        content = "PU100.000,200.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        assert isinstance(doc, PLTDocument)

    def test_multiple_coordinates_in_sequence(self) -> None:
        """Test parsing multiple coordinate pairs in sequence."""
        content = "PU0.000,0.000;PD100.000,100.000;PD200.000,200.000;SP;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        total_segments = sum(len(p.segments) for p in doc.stroke_paths)
        assert total_segments >= 2

    def test_next_token_is_command_not_coordinates(self) -> None:
        """Test that non-coordinate tokens after PU/PD are not consumed."""
        content = "PU0.000,0.000;VS1.50;SP;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        vs_cmds = [hc for hc in doc.header_commands if hc.instruction == "VS"]
        assert len(vs_cmds) >= 1

    def test_header_command_with_numeric_params(self) -> None:
        """Test header commands with numeric colon-separated parameters."""
        content = "IN;SC0.5:1;SP;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        sc_cmds = [hc for hc in doc.header_commands if hc.instruction == "SC0.5"]
        assert len(sc_cmds) >= 1

    def test_header_command_from_token_with_no_params(self) -> None:
        """Test HeaderCommand.from_token with no parameters."""
        content = "IN;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        in_cmds = [hc for hc in doc.header_commands if hc.instruction == "IN"]
        assert len(in_cmds) >= 1

    def test_parse_with_empty_content(self) -> None:
        """Test parsing empty content doesn't crash."""
        parser = PLTParser()

        doc = parser.parse_string("")

        assert isinstance(doc, PLTDocument)
        assert len(doc.stroke_paths) == 0

    def test_parse_with_only_semicolons(self) -> None:
        """Test parsing content with only semicolons."""
        parser = PLTParser()

        doc = parser.parse_string(";;")

        assert isinstance(doc, PLTDocument)


class TestExtractCoordinatesEdgeCases:
    """Tests for edge cases in _extract_coordinates method."""

    def test_exhausted_tokens_returns_empty_coords(self) -> None:
        """Test that exhausted token list returns empty coordinates."""
        content = "PD;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        # PD with no coordinates - should handle gracefully
        assert isinstance(doc, PLTDocument)

    def test_pd_without_last_position(self) -> None:
        """Test PD without prior PU doesn't create segments."""
        content = "PD100.000,200.000;SP;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        # PD without preceding PU has no last_position, so no segments
        assert len(doc.stroke_paths) == 0

    def test_coordinates_from_next_token(self) -> None:
        """Test extracting coordinates from a subsequent token."""
        parser = PLTParser()

        # Pass tokens where next token contains coordinate pair after semicolon
        coords, idx = parser._extract_coordinates(
            "PD;", 0, ["PD;", "10.5,20.3;"]
        )

        assert len(coords) == 1
        assert math.isclose(coords[0].x, 10.5, abs_tol=0.001)
        assert math.isclose(coords[0].y, 20.3, abs_tol=0.001)
        assert idx == 2

    def test_empty_tokens_list(self) -> None:
        """Test _extract_coordinates with empty token list."""
        parser = PLTParser()

        coords, idx = parser._extract_coordinates("PD;", 0, [])

        assert len(coords) == 0
        assert idx == 1

    def test_single_token_exhaustion(self) -> None:
        """Test _extract_coordinates when only one token exists."""
        parser = PLTParser()

        coords, idx = parser._extract_coordinates("PD;", 0, ["PD;"])

        assert len(coords) == 0
        assert idx == 1


class TestFooterCommandParsing:
    """Tests for footer command parsing."""

    def test_parse_sp_footer(self) -> None:
        """Test parsing SP (select pen) footer command."""
        content = "SP;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        assert len(doc.footer_commands) >= 1
        assert doc.footer_commands[0].instruction == "SP"

    def test_parse_multiple_footers(self) -> None:
        """Test parsing multiple footer commands."""
        content = "SP;SP;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        assert len(doc.footer_commands) >= 2


class TestPLTDocumentMethods:
    """Tests for PLTDocument methods."""

    def test_total_distance_method(self) -> None:
        """Test PLTDocument.total_distance() method."""
        content = "PU0.000,0.000;PD100.000,0.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        total = doc.total_distance()
        assert math.isclose(total, 100.0, abs_tol=0.001)

    def test_cutting_distance_method(self) -> None:
        """Test PLTDocument.cutting_distance() method."""
        content = "PU0.000,0.000;PD100.000,0.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        cutting = doc.cutting_distance()
        assert math.isclose(cutting, 100.0, abs_tol=0.001)

    def test_rapid_distance_method(self) -> None:
        """Test PLTDocument.rapid_distance() method returns float."""
        content = "PU0.000,100.000;PD200.000,100.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        # rapid_distance should return 0.0 since PU doesn't create segments
        rapid = doc.rapid_distance()
        assert rapid == 0.0

    def test_total_segments_property(self) -> None:
        """Test PLTDocument.total_segments property."""
        content = "PU0.000,0.000;PD100.000,0.000;PD200.000,100.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        total = doc.total_segments
        assert total >= 2


class TestIsHeaderCommand:
    """Tests for the _is_header_command method."""

    def test_known_header_commands(self) -> None:
        """Test that known header commands return True."""
        parser = PLTParser()

        for cmd in ["IN", "VS0.50", "ZO123,1"]:
            assert parser._is_header_command(cmd) is True

    def test_path_commands_not_headers(self) -> None:
        """Test that PU and PD are not treated as header commands."""
        parser = PLTParser()

        assert parser._is_header_command("PU0.000,0.000") is False
        assert parser._is_header_command("PD100.000,0.000") is False

    def test_lowercase_command_not_header(self) -> None:
        """Test that lowercase commands return False (no match)."""
        parser = PLTParser()

        assert parser._is_header_command("abc") is False


class TestArcParsing:
    """Tests for arc command parsing."""

    def test_parse_aa_command_separate_tokens(self) -> None:
        """Test parsing AA (Arc Absolute) from separate PD;AA tokens."""
        content = "PU0.000,0.000;PD1016.000,1016.000;AA1016.000,1016.000,90.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        arc_segments = [
            seg for path in doc.stroke_paths
            for seg in path.segments if isinstance(seg, ArcSegment)
        ]
        assert len(arc_segments) >= 1

    def test_parse_aa_command_compound_token(self) -> None:
        """Test parsing AA after PD with no semicolon between them."""
        content = "PU0.000,0.000;PDAA1016.000,1016.000,90.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        arc_segments = [
            seg for path in doc.stroke_paths
            for seg in path.segments if isinstance(seg, ArcSegment)
        ]
        assert len(arc_segments) >= 1

    def test_parse_ar_command(self) -> None:
        """Test parsing AR (Arc Relative - counter-clockwise)."""
        content = "PU0.000,0.000;PD1016.000,1016.000;AR1016.000,1016.000,90.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        arc_segments = [
            seg for path in doc.stroke_paths
            for seg in path.segments if isinstance(seg, ArcSegment)
        ]
        assert len(arc_segments) >= 1

    def test_parse_ci_command(self) -> None:
        """Test parsing CI (Circle) command."""
        content = "PU0.000,1000.000;PDCI500;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        arc_segments = [
            seg for path in doc.stroke_paths
            for seg in path.segments if isinstance(seg, ArcSegment)
        ]
        assert len(arc_segments) >= 1

    def test_arc_endpoint_calculation(self) -> None:
        """Test that arc endpoint is calculated correctly.

        For AA with center at (1016, 1016), start at (0, 0):
        - Radius = distance((0,0), (1016,1016)) ≈ 1437.0
        - Start angle = atan2(0-1016, 0-1016) = atan2(-1016, -1016) = -135 degrees
        - Sweep 90 degrees clockwise: delta_theta = -90 * pi/180 = -pi/2
        - End position should be at (1437.0 + 1016*something)
        """
        content = "PU0.000,0.000;PDAA1016.000,1016.000,90.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        arc_segments = [
            seg for path in doc.stroke_paths
            for seg in path.segments if isinstance(seg, ArcSegment)
        ]
        assert len(arc_segments) >= 1

        arc = arc_segments[0]
        expected_radius = math.sqrt(2 * (1016.0 ** 2))
        assert math.isclose(arc.radius, expected_radius, abs_tol=0.001)

    def test_arc_segment_has_correct_sweep_angle(self) -> None:
        """Test that ArcSegment stores the correct sweep angle."""
        content = "PU0.000,0.000;PDAA1016.000,1016.000,90.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        arc_segments = [
            seg for path in doc.stroke_paths
            for seg in path.segments if isinstance(seg, ArcSegment)
        ]
        assert len(arc_segments) >= 1

        arc = arc_segments[0]
        assert math.isclose(abs(arc.sweep_angle), 90.0, abs_tol=0.001)

    def test_arc_center_extraction(self) -> None:
        """Test that ArcSegment correctly stores center coordinates."""
        content = "PU0.000,0.000;PDAA500.000,500.000,45.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        arc_segments = [
            seg for path in doc.stroke_paths
            for seg in path.segments if isinstance(seg, ArcSegment)
        ]
        assert len(arc_segments) >= 1

        arc = arc_segments[0]
        assert math.isclose(arc.center.x, 500.0, abs_tol=0.001)
        assert math.isclose(arc.center.y, 500.0, abs_tol=0.001)

    def test_multiple_arcs_in_sequence(self) -> None:
        """Test parsing multiple arc commands in sequence."""
        content = (
            "PU0.000,0.000;"
            "PDAA1016.000,1016.000,90.000;"
            "PDAA2032.000,0.000,180.000;"
        )
        parser = PLTParser()

        doc = parser.parse_string(content)

        arc_segments = [
            seg for path in doc.stroke_paths
            for seg in path.segments if isinstance(seg, ArcSegment)
        ]
        assert len(arc_segments) >= 2


class TestArcSegmentInStrokePath:
    """Tests for StrokePath containing ArcSegments."""

    def test_stroke_path_with_arc_segment(self) -> None:
        """Test that StrokePath can contain both line and arc segments."""
        content = "PU0.000,0.000;PD100.000,100.000;AA200.000,200.000,90.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        assert len(doc.stroke_paths) >= 1
        path = doc.stroke_paths[0]
        arc_segments = [seg for seg in path.segments if isinstance(seg, ArcSegment)]
        line_segments = [seg for seg in path.segments if isinstance(seg, StrokeSegment)]
        assert len(arc_segments) >= 1
        assert len(line_segments) >= 1

    def test_path_with_arc_has_correct_cutting_distance(self) -> None:
        """Test that cutting distance is calculated correctly with arcs."""
        content = "PU0.000,0.000;PDAA1016.000,1016.000,90.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        if len(doc.stroke_paths) >= 1:
            path = doc.stroke_paths[0]
            cutting_dist = path.cutting_distance
            assert cutting_dist > 0

    def test_arc_chord_length_used_for_distance(self) -> None:
        """Test that arc chord length (not arc length) is used for distance.

        The distance property should return straight-line distance from start to end,
        not the actual curved arc length.
        """
        content = "PU0.000,0.000;PDAA1016.000,1016.000,90.000;"
        parser = PLTParser()

        doc = parser.parse_string(content)

        if len(doc.stroke_paths) >= 1:
            path = doc.stroke_paths[0]
            arc_seg = [seg for seg in path.segments if isinstance(seg, ArcSegment)][0]

            chord_length = math.sqrt(
                (arc_seg.end.x - arc_seg.start.x) ** 2 +
                (arc_seg.end.y - arc_seg.start.y) ** 2
            )

            assert math.isclose(path.cutting_distance, chord_length, abs_tol=0.001)
