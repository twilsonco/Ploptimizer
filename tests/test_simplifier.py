"""Tests for stroke simplification helper functions.

These tests cover:
- is_point_on_segment() utility in plt_optimizer/utils/geometry.py

The document-level deduplication tests live in tests/test_stroke_simplifier.py
against plt_optimizer/core/stroke_simplifier.py.
"""

from __future__ import annotations

from plt_optimizer.core.models import Coordinate


class TestIsPointOnSegment:
    """Tests for is_point_on_segment function."""

    def test_point_on_horizontal_segment(self) -> None:
        """Test point on horizontal segment."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=0.0, y=0.0)
        b = Coordinate(x=10.0, y=0.0)
        p = Coordinate(x=5.0, y=0.0)

        assert is_point_on_segment(p, a, b) is True

    def test_point_on_vertical_segment(self) -> None:
        """Test point on vertical segment."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=5.0, y=-10.0)
        b = Coordinate(x=5.0, y=20.0)
        p = Coordinate(x=5.0, y=5.0)

        assert is_point_on_segment(p, a, b) is True

    def test_point_at_start_endpoint(self) -> None:
        """Test point at segment start endpoint."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=1.0, y=2.0)
        b = Coordinate(x=10.0, y=20.0)
        p = Coordinate(x=1.0, y=2.0)

        assert is_point_on_segment(p, a, b) is True

    def test_point_at_end_endpoint(self) -> None:
        """Test point at segment end endpoint."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=1.0, y=2.0)
        b = Coordinate(x=10.0, y=20.0)
        p = Coordinate(x=10.0, y=20.0)

        assert is_point_on_segment(p, a, b) is True

    def test_point_outside_segment_bounds_collinear(self) -> None:
        """Test point collinear but beyond segment bounds."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=0.0, y=0.0)
        b = Coordinate(x=10.0, y=0.0)
        p = Coordinate(x=15.0, y=0.0)

        assert is_point_on_segment(p, a, b) is False

    def test_point_before_start_collinear(self) -> None:
        """Test point before start of segment but collinear."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=5.0, y=5.0)
        b = Coordinate(x=10.0, y=10.0)
        p = Coordinate(x=0.0, y=0.0)

        assert is_point_on_segment(p, a, b) is False

    def test_point_not_collinear(self) -> None:
        """Test point not on line (different slope)."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=0.0, y=0.0)
        b = Coordinate(x=10.0, y=0.0)
        p = Coordinate(x=5.0, y=1.0)

        assert is_point_on_segment(p, a, b) is False

    def test_zero_length_segment_point(self) -> None:
        """Test with zero-length segment (point)."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        p = Coordinate(x=5.0, y=5.0)
        a = Coordinate(x=5.0, y=5.0)
        b = Coordinate(x=5.0, y=5.0)

        result = is_point_on_segment(p, a, b)
        assert result is True

    def test_zero_length_segment_different_point(self) -> None:
        """Test zero-length segment with different point."""
        from plt_optimizer.utils.geometry import is_point_on_segment

        a = Coordinate(x=5.0, y=5.0)
        b = Coordinate(x=5.0, y=5.0)
        p = Coordinate(x=10.0, y=10.0)

        assert is_point_on_segment(p, a, b) is False
