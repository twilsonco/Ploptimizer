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
- ArcSegment.radius / chord_length / length properties
- StrokePath.chord_distance property
- _segment_length unified dispatch helper (regression for AttributeError)
"""

from __future__ import annotations

import math
import types
import typing

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


class TestArcSegmentProperties:
    """Tests for ArcSegment length-related properties.

    Verifies the geometry invariants of the new ``length`` (true arc length),
    ``chord_length`` (straight-line approximation), and ``radius`` properties.
    These tests guard against the historical ``AttributeError`` that occurred
    when ``ArcSegment`` lacked a ``.length`` property entirely.
    """

    def test_radius_equals_distance_from_start_to_center(self) -> None:
        """ArcSegment.radius equals the Euclidean distance from start to center."""
        from plt_optimizer.core.models import ArcSegment, Coordinate

        arc = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=0.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        assert arc.radius == pytest.approx(5.0)

    def test_chord_length_equals_euclidean_start_to_end(self) -> None:
        """ArcSegment.chord_length equals the straight-line distance from start to end."""
        from plt_optimizer.core.models import ArcSegment, Coordinate

        arc = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=10.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        expected = math.sqrt(10.0 ** 2 + 10.0 ** 2)
        assert arc.chord_length == pytest.approx(expected)

    def test_length_is_true_arc_length_quarter_circle(self) -> None:
        """A 90-degree sweep at radius 5 produces an arc length of 2*pi*r/4.

        This is the canonical case where the true arc length substantially
        differs from the chord length (radius * sqrt(2) ≈ 7.07 vs 2*pi*5/4 ≈ 7.854).
        """
        from plt_optimizer.core.models import ArcSegment, Coordinate

        arc = ArcSegment(
            start=Coordinate(x=5.0, y=0.0),
            end=Coordinate(x=0.0, y=5.0),
            center=Coordinate(x=0.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        expected = 2.0 * math.pi * 5.0 / 4.0
        assert arc.length == pytest.approx(expected)
        # Sanity check: true length must exceed the chord length for non-trivial sweeps
        assert arc.length > arc.chord_length

    def test_length_is_true_arc_length_semicircle(self) -> None:
        """A 180-degree sweep at radius 10 produces pi * 10."""
        from plt_optimizer.core.models import ArcSegment, Coordinate

        arc = ArcSegment(
            start=Coordinate(x=10.0, y=0.0),
            end=Coordinate(x=-10.0, y=0.0),
            center=Coordinate(x=0.0, y=0.0),
            sweep_angle=180.0,
            is_cutting=True,
        )
        expected = math.pi * 10.0
        assert arc.length == pytest.approx(expected)

    def test_length_discards_sign_of_sweep_angle(self) -> None:
        """Length must be non-negative; the sign encodes direction only.

        A negative sweep of the same magnitude must yield the same length.
        """
        from plt_optimizer.core.models import ArcSegment, Coordinate

        arc_cw = ArcSegment(
            start=Coordinate(x=5.0, y=0.0),
            end=Coordinate(x=0.0, y=5.0),
            center=Coordinate(x=0.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        arc_ccw = ArcSegment(
            start=Coordinate(x=5.0, y=0.0),
            end=Coordinate(x=0.0, y=5.0),
            center=Coordinate(x=0.0, y=0.0),
            sweep_angle=-90.0,
            is_cutting=True,
        )
        assert arc_cw.length == pytest.approx(arc_ccw.length)
        assert arc_cw.length >= 0.0
        assert arc_ccw.length >= 0.0

    def test_length_zero_when_sweep_zero(self) -> None:
        """Zero-degree sweep yields zero length regardless of radius."""
        from plt_optimizer.core.models import ArcSegment, Coordinate

        arc = ArcSegment(
            start=Coordinate(x=5.0, y=0.0),
            end=Coordinate(x=5.0, y=0.0),
            center=Coordinate(x=0.0, y=0.0),
            sweep_angle=0.0,
            is_cutting=True,
        )
        assert arc.length == pytest.approx(0.0)


class TestSegmentLengthUnifiedDispatch:
    """Regression tests for the unified ``_segment_length`` helper.

    These tests verify that the historical ``AttributeError: 'ArcSegment' object
    has no attribute 'length'`` cannot resurface: any ``Segment`` instance now
    exposes a uniform ``.length`` property.
    """

    def test_arc_segment_has_length_attribute(self) -> None:
        """ArcSegment must expose a ``length`` property."""
        from plt_optimizer.core.models import ArcSegment, Coordinate

        arc = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=10.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        # Accessing the property must not raise AttributeError
        assert isinstance(arc.length, float)
        assert arc.length > 0

    def test_stroke_segment_length_unchanged(self) -> None:
        """StrokeSegment.length remains the Euclidean length of the segment."""
        from plt_optimizer.core.models import Coordinate

        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        assert seg.length == pytest.approx(100.0)

    def test_segment_length_helper_dispatches_for_arc(self) -> None:
        """The ``_segment_length`` helper must work for ArcSegment."""
        from plt_optimizer.core.models import ArcSegment, Coordinate, _segment_length

        arc = ArcSegment(
            start=Coordinate(x=5.0, y=0.0),
            end=Coordinate(x=0.0, y=5.0),
            center=Coordinate(x=0.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        # Must equal the true arc length, not the chord
        assert _segment_length(arc) == pytest.approx(arc.length)
        assert _segment_length(arc) > arc.chord_length

    def test_segment_length_helper_dispatches_for_stroke(self) -> None:
        """The ``_segment_length`` helper must work for StrokeSegment."""
        from plt_optimizer.core.models import Coordinate, _segment_length

        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=42.0, y=0.0),
            is_cutting=True,
        )
        assert _segment_length(seg) == pytest.approx(42.0)

class TestSegmentAliasPython38Compatibility:
    """Regression tests for the ``Segment`` alias runtime type.

    The ``Segment`` alias in ``plt_optimizer.core.models`` is evaluated at
    module import time. PEP 604 union syntax (``X | Y``) is only valid at
    runtime on Python 3.10+; on Python 3.8/3.9 it raises
    ``TypeError: unsupported operand type(s) for |: 'type' and 'type'``,
    which crashes the PyInstaller-built Windows EXE on Windows 7.

    These tests pin the alias to ``typing.Union`` so the module imports
    cleanly on the Python 3.8 floor.
    """

    def test_segment_alias_is_importable(self) -> None:
        """``Segment`` must be importable from ``plt_optimizer.core.models``."""
        from plt_optimizer.core.models import Segment

        assert Segment is not None

    def test_segment_alias_is_not_pep604_runtime_union(self) -> None:
        """``Segment`` must NOT be a ``types.UnionType`` (PEP 604 runtime union).

        ``types.UnionType`` only exists on Python 3.10+. If ``Segment`` is
        a ``types.UnionType``, the module fails to import on Python 3.8/3.9
        and the Windows 7 EXE crashes at startup with
        ``TypeError: unsupported operand type(s) for |: 'type' and 'type'``.
        """
        from plt_optimizer.core.models import Segment

        # types.UnionType is the runtime type of the PEP 604 'X | Y' expression.
        # It only exists on Python 3.10+. On 3.8/3.9, the attribute itself
        # is absent, which is itself the regression signal.
        assert not hasattr(types, "UnionType") or not isinstance(
            Segment, types.UnionType
        )

    def test_segment_alias_resolves_to_both_concrete_types(self) -> None:
        """``typing.get_args(Segment)`` must include both concrete segment types."""
        from plt_optimizer.core.models import ArcSegment, Segment

        args = typing.get_args(Segment)
        assert ArcSegment in args
        assert StrokeSegment in args

    def test_segment_alias_accepts_isinstance_for_both_types(self) -> None:
        """``isinstance(x, Segment)`` must work for both ``ArcSegment`` and ``StrokeSegment``."""
        from plt_optimizer.core.models import ArcSegment, Coordinate, Segment

        arc = ArcSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=10.0, y=10.0),
            center=Coordinate(x=5.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        stroke = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=42.0, y=0.0),
            is_cutting=True,
        )
        assert isinstance(arc, Segment)
        assert isinstance(stroke, Segment)

class TestStrokePathChordDistance:
    """Tests for the StrokePath.chord_distance property (straight-line approx)."""

    def test_chord_distance_mixed_segments(self) -> None:
        """chord_distance sums chord lengths for arcs and line lengths for strokes."""
        from plt_optimizer.core.models import ArcSegment, Coordinate

        line = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        arc = ArcSegment(
            start=Coordinate(x=5.0, y=0.0),
            end=Coordinate(x=0.0, y=5.0),
            center=Coordinate(x=0.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        path = StrokePath(segments=(line, arc))

        expected = line.length + arc.chord_length
        assert path.chord_distance == pytest.approx(expected)

    def test_chord_distance_does_not_equal_total_distance_for_arcs(self) -> None:
        """For paths with arcs, total_distance (true length) > chord_distance."""
        from plt_optimizer.core.models import ArcSegment, Coordinate

        arc = ArcSegment(
            start=Coordinate(x=5.0, y=0.0),
            end=Coordinate(x=0.0, y=5.0),
            center=Coordinate(x=0.0, y=0.0),
            sweep_angle=90.0,
            is_cutting=True,
        )
        path = StrokePath(segments=(arc,))
        assert path.total_distance == pytest.approx(arc.length)
        assert path.chord_distance == pytest.approx(arc.chord_length)
        assert path.total_distance > path.chord_distance

    def test_chord_distance_empty_path(self) -> None:
        """Empty path has zero chord distance."""
        path = StrokePath()
        assert path.chord_distance == 0.0


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

    def test_single_path_no_rapid(self) -> None:
        """Test rapid distance with single path (no between-path travel)."""
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=50.0, y=0.0),
            is_cutting=True,
        )
        path = StrokePath(segments=(seg,), pen_up_position=Coordinate(x=0.0, y=0.0))
        doc = PLTDocument(stroke_paths=[path])

        assert doc.rapid_distance() == 0.0

    def test_two_paths_with_rapid_between(self) -> None:
        """Test rapid distance calculation between consecutive paths.

        In HPGL, rapid (pen-up) moves occur when moving from one path's end
        to the next path's pen_up_position.
        Path1 ends at (100, 0), Path2 pen_up is at (200, 100).
        Distance = sqrt((200-100)^2 + (100-0)^2) = sqrt(10000+10000) ≈ 141.42
        """
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        path1 = StrokePath(segments=(seg1,), pen_up_position=Coordinate(x=0.0, y=0.0))

        seg2 = StrokeSegment(
            start=Coordinate(x=200.0, y=100.0),
            end=Coordinate(x=300.0, y=100.0),
            is_cutting=True,
        )
        path2 = StrokePath(segments=(seg2,), pen_up_position=Coordinate(x=200.0, y=100.0))

        doc = PLTDocument(stroke_paths=[path1, path2])

        expected_rapid = 141.4213562373095
        assert doc.rapid_distance() == pytest.approx(expected_rapid)

    def test_three_paths_accumulates_rapid(self) -> None:
        """Test rapid distance accumulates between all consecutive paths."""
        from plt_optimizer.core.models import Coordinate, StrokeSegment

        seg1 = StrokeSegment(
            start=Coordinate(x=0.0, y=0.0),
            end=Coordinate(x=100.0, y=0.0),
            is_cutting=True,
        )
        path1 = StrokePath(segments=(seg1,), pen_up_position=Coordinate(x=0.0, y=0.0))

        seg2 = StrokeSegment(
            start=Coordinate(x=200.0, y=100.0),
            end=Coordinate(x=300.0, y=100.0),
            is_cutting=True,
        )
        path2 = StrokePath(segments=(seg2,), pen_up_position=Coordinate(x=200.0, y=100.0))

        seg3 = StrokeSegment(
            start=Coordinate(x=500.0, y=50.0),
            end=Coordinate(x=600.0, y=50.0),
            is_cutting=True,
        )
        path3 = StrokePath(segments=(seg3,), pen_up_position=Coordinate(x=500.0, y=50.0))

        doc = PLTDocument(stroke_paths=[path1, path2, path3])

        dist_1_to_2 = 141.4213562373095
        dist_2_to_3 = 206.1552812819245
        expected_rapid = dist_1_to_2 + dist_2_to_3
        assert doc.rapid_distance() == pytest.approx(expected_rapid)


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
