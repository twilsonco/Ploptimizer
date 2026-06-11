"""Tests for plt_optimizer/core/models.py model classes.

These tests target specific lines not covered by existing identity/parser/writer tests:
- Coordinate.from_string() line 64
- HeaderCommand from_token regex fallback lines 93-96, return line 109
- HeaderCommand.__post_object__ rounding lines 115-116
- HeaderCommand.format() no params line 128-129
- StrokePath.is_empty property line 188
- StrokePath.rapid_distance property line 204
- FooterCommand.from_token lines 216-219
- PLTDocument.rapid_distance() line 242
"""

from __future__ import annotations

import pytest

from plt_optimizer.core.models import (
    PLTDocument,
    StrokePath,
    StrokeSegment,
)


class TestCoordinateFromStrings:
    """Tests for Coordinate.from_string() method (line 64)."""

    def test_parse_integer_coordinates(self) -> None:
        """Test parsing integer string coordinates."""
        from plt_optimizer.core.models import Coordinate

        coord = Coordinate.from_string("100", "200")
        assert coord.x == 100.0
        assert coord.y == 200.0

    def test_parse_float_coordinates(self) -> None:
        """Test parsing float string coordinates."""
        from plt_optimizer.core.models import Coordinate

        coord = Coordinate.from_string("18288.5", "-0.125")
        assert coord.x == 18288.5
        assert coord.y == -0.125

    def test_parse_precision_preserved(self) -> None:
        """Test that 3 decimal place precision is maintained from strings."""
        from plt_optimizer.core.models import Coordinate

        coord = Coordinate.from_string("123.456789", "987.654321")
        assert coord.x == 123.457
        assert coord.y == 987.654


class TestHeaderCommandFromToken:
    """Tests for HeaderCommand.from_token() edge cases."""

    def test_parse_command_with_colon_separator(self) -> None:
        """Test parsing a command with : separator (line 109)."""
        from plt_optimizer.core.models import HeaderCommand

        cmd = HeaderCommand.from_token("VS0.50;")
        assert cmd.instruction == "VS"
        assert cmd.parameters is not None
        assert len(cmd.parameters) == 1

    def test_parse_command_with_colon_and_multiple_params(self) -> None:
        """Test parsing a command with multiple params via : separator."""
        from plt_optimizer.core.models import HeaderCommand

        cmd = HeaderCommand.from_token("ZO123,456;")
        assert cmd.instruction == "ZO"
        assert cmd.parameters is not None
        assert cmd.parameters == (123.0, 456.0)

    def test_parse_command_no_params(self) -> None:
        """Test parsing a command with no params via regex fallback (line 93-96)."""
        from plt_optimizer.core.models import HeaderCommand

        cmd = HeaderCommand.from_token("IN;")
        assert cmd.instruction == "IN"
        assert cmd.parameters is None

    def test_parse_command_no_params_various(self) -> None:
        """Test various no-param commands go through regex fallback (line 109)."""
        from plt_optimizer.core.models import HeaderCommand

        for token in ["PA;", "PU0.000,0.000;"]:
            cmd = HeaderCommand.from_token(token)
            # Commands without explicit : separator use regex fallback

    def test_parse_command_params_rounded(self) -> None:
        """Test that floating point params are rounded to 3 decimal places (line 115-116)."""
        from plt_optimizer.core.models import HeaderCommand

        cmd = HeaderCommand.from_token("VS0.5678;")
        assert cmd.parameters == (0.568,)


class TestHeaderCommandFormat:
    """Tests for HeaderCommand.format() method (lines 128-129)."""

    def test_format_no_parameters(self) -> None:
        """Test formatting a command with no parameters."""
        from plt_optimizer.core.models import HeaderCommand

        cmd = HeaderCommand(instruction="IN")
        result = cmd.format()
        assert result == "IN;"

    def test_format_with_parameters(self) -> None:
        """Test formatting a command with parameters."""
        from plt_optimizer.core.models import HeaderCommand

        cmd = HeaderCommand(instruction="VS", parameters=(0.5, 1.0))
        result = cmd.format()
        assert "VS" in result


class TestStrokePathIsEmpty:
    """Tests for StrokePath.is_empty property (line 188)."""

    def test_empty_path(self) -> None:
        """Test is_empty for a path with no segments."""
        path = StrokePath()
        assert path.is_empty is True

    def test_nonempty_path(self) -> None:
        """Test is_empty for a path with segments."""
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        assert path.is_empty is False


class TestStrokePathRapidDistance:
    """Tests for StrokePath.rapid_distance property (line 204)."""

    def test_rapid_distance_only(self) -> None:
        """Test rapid distance calculation when all segments are pen-up."""
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=False,  # pen up = rapid move
        )
        path = StrokePath(segments=(seg,))
        assert path.rapid_distance == 100.0

    def test_mixed_rapid_and_cutting(self) -> None:
        """Test rapid distance excludes cutting segments."""
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=False,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=100.0, y=0.0),
            end=Coordinate(x=200.0, y=0.0),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg1, seg2))
        assert path.rapid_distance == 100.0


class TestFooterCommandFromToken:
    """Tests for FooterCommand.from_token() method (lines 216-219)."""

    def test_parse_sp_command(self) -> None:
        """Test parsing SP footer command."""
        from plt_optimizer.core.models import FooterCommand

        cmd = FooterCommand.from_token("SP;")
        assert cmd.instruction == "SP"

    def test_parse_pg_command(self) -> None:
        """Test parsing PG footer command."""
        from plt_optimizer.core.models import FooterCommand

        cmd = FooterCommand.from_token("PG;")
        assert cmd.instruction == "PG"

    def test_parse_format_footers(self) -> None:
        """Test formatting footer commands."""
        from plt_optimizer.core.models import FooterCommand

        cmd = FooterCommand(instruction="SP")
        assert cmd.format() == "SP;"


class TestPLTDocumentRapidDistance:
    """Tests for PLTDocument.rapid_distance() method (line 242)."""

    def test_empty_document_rapid(self) -> None:
        """Test rapid distance on empty document."""
        doc = PLTDocument()
        assert doc.rapid_distance() == 0.0

    def test_document_with_rapid_segments(self) -> None:
        """Test rapid distance calculation across document paths."""
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=50.0, y=0.0),
            is_cutting=False,
        )
        path = StrokePath(segments=(seg,))
        doc = PLTDocument(stroke_paths=[path])

        assert doc.rapid_distance() == 50.0

    def test_document_mixed_segments_rapid(self) -> None:
        """Test rapid distance excludes cutting segments in document."""
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=False,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=100.0, y=0.0),
            end=Coordinate(x=200.0, y=0.0),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg1, seg2))
        doc = PLTDocument(stroke_paths=[path])

        assert doc.rapid_distance() == 100.0


class TestPLTDocumentCuttingDistance:
    """Tests for PLTDocument.cutting_distance() method (line 246)."""

    def test_empty_document_cutting(self) -> None:
        """Test cutting distance on empty document."""
        doc = PLTDocument()
        assert doc.cutting_distance() == 0.0

    def test_document_with_cutting_segments(self) -> None:
        """Test cutting distance calculation across document paths."""
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=50.0, y=0.0),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        doc = PLTDocument(stroke_paths=[path])

        assert doc.cutting_distance() == 50.0

    def test_document_mixed_segments_cutting(self) -> None:
        """Test cutting distance excludes rapid segments in document."""
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=False,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=100.0, y=0.0),
            end=Coordinate(x=200.0, y=0.0),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg1, seg2))
        doc = PLTDocument(stroke_paths=[path])

        assert doc.cutting_distance() == 100.0


class TestPLTDocumentTotalDistance:
    """Tests for PLTDocument.total_distance() method (line 242)."""

    def test_empty_document_total(self) -> None:
        """Test total distance on empty document."""
        doc = PLTDocument()
        assert doc.total_distance() == 0.0

    def test_document_total_single_path(self) -> None:
        """Test total distance calculation across document paths."""
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=50.0, y=0.0),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        doc = PLTDocument(stroke_paths=[path])

        assert doc.total_distance() == 50.0


class TestPLTDocumentTotalSegments:
    """Tests for PLTDocument.total_segments property (line 255)."""

    def test_empty_document_segments(self) -> None:
        """Test total segments on empty document."""
        doc = PLTDocument()
        assert doc.total_segments == 0

    def test_document_multiple_paths(self) -> None:
        """Test total segments across multiple paths."""
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=100.0, y=0.0),
            end=Coordinate(x=200.0, y=0.0),
            is_cutting=True,
        )
        path1 = StrokePath(segments=(seg1,))
        path2 = StrokePath(segments=(seg2,))
        doc = PLTDocument(stroke_paths=[path1, path2])

        assert doc.total_segments == 2


class TestStrokePathTotalDistance:
    """Tests for StrokePath.total_distance property (line 176, 181)."""

    def test_single_segment_total(self) -> None:
        """Test total distance with single segment."""
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=3.0, y=4.0),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))

        assert path.total_distance == 5.0

    def test_multi_segment_total(self) -> None:
        """Test total distance with multiple segments."""
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        seg2 = StrokeSegment(
            start=Coordinate(x=100.0, y=0.0),
            end=Coordinate(x=100.0, y=100.0),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg1, seg2))

        assert path.total_distance == 200.0


class TestCoordinateAsTuple:
    """Tests for Coordinate.as_tuple() method (line 64)."""

    def test_as_tuple_basic(self) -> None:
        """Test as_tuple returns correct tuple."""
        from plt_optimizer.core.models import Coordinate

        coord = Coordinate(x=100.5, y=-200.75)
        result = coord.as_tuple()
        assert result == (100.5, -200.75)

    def test_as_tuple_rounded_values(self) -> None:
        """Test as_tuple returns rounded values."""
        from plt_optimizer.core.models import Coordinate

        coord = Coordinate(x=123.456789, y=-987.654321)
        result = coord.as_tuple()
        assert result == (123.457, -987.654)


class TestHeaderCommandFormatNoParams:
    """Tests for HeaderCommand.format() no-params branch (lines 128-129)."""

    def test_format_no_params_branch(self) -> None:
        """Test formatting a command with no parameters (else branch)."""
        from plt_optimizer.core.models import HeaderCommand

        cmd = HeaderCommand(instruction="PA")
        result = cmd.format()
        assert result == "PA;"


class TestHeaderCommandFromTokenRegexFallback:
    """Tests for HeaderCommand.from_token() regex fallback (lines 128-129)."""

    def test_parse_no_colon_command(self) -> None:
        """Test parsing a command without : separator goes through regex."""
        from plt_optimizer.core.models import HeaderCommand

        cmd = HeaderCommand.from_token("PA;")
        assert cmd.instruction == "PA"
        assert cmd.parameters is None

    def test_parse_no_colon_with_params(self) -> None:
        """Test parsing a command without : but with params via regex."""
        from plt_optimizer.core.models import HeaderCommand

        cmd = HeaderCommand.from_token("PA0.5;")
        assert cmd.instruction == "PA"
        assert cmd.parameters is not None

    def test_parse_invalid_token_raises(self) -> None:
        """Test parsing token without semicolon raises ValueError."""
        from plt_optimizer.core.models import HeaderCommand

        with pytest.raises(ValueError, match="Invalid token format"):
            HeaderCommand.from_token("INVALID")

    def test_parse_footer_command_raises(self) -> None:
        """Test parsing footer-style token raises ValueError (line 217)."""
        from plt_optimizer.core.models import HeaderCommand

        with pytest.raises(ValueError, match="Invalid token format"):
            # Footer commands like SP; don't have : separator and go through regex
            HeaderCommand.from_token("SP")

    def test_parse_non_alpha_regex_fallback(self) -> None:
        """Test parsing a token that doesn't match regex goes to else branch (lines 128-129)."""
        from plt_optimizer.core.models import HeaderCommand

        # A token with no uppercase letters won't match the regex, triggering else branch
        cmd = HeaderCommand.from_token("123;")
        assert cmd.instruction == "123"
        assert cmd.parameters is None

    def test_parse_params_via_colon_branch(self) -> None:
        """Test parsing params via : colon branch (lines 115-116)."""
        from plt_optimizer.core.models import HeaderCommand

        cmd = HeaderCommand.from_token("VS0.5678;")
        assert cmd.parameters == (0.568,)

    def test_parse_empty_params_via_colon(self) -> None:
        """Test parsing with colon but empty params (lines 115-116 branch)."""
        from plt_optimizer.core.models import HeaderCommand

        # X: splits into ["X", ""], param_str is "", split(",") gives [""]
        # float("") raises ValueError, hitting lines 115-116
        with pytest.raises(ValueError):
            HeaderCommand.from_token("X:;")

    def test_parse_footer_command_no_semicolon_raises(self) -> None:
        """Test FooterCommand.from_token without semicolon raises ValueError (line 217)."""
        from plt_optimizer.core.models import FooterCommand

        with pytest.raises(ValueError, match="Invalid token format"):
            FooterCommand.from_token("SP")


class TestPLTDocumentTotalDistance:
    """Tests for PLTDocument.total_distance() method (line 242)."""

    def test_empty_document_total(self) -> None:
        """Test total distance on empty document."""
        doc = PLTDocument()
        assert doc.total_distance() == 0.0

    def test_document_total_single_path(self) -> None:
        """Test total distance calculation across document paths."""
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=50.0, y=0.0),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,))
        doc = PLTDocument(stroke_paths=[path])

        assert doc.total_distance() == 50.0
