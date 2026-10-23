"""Tests for the list marker composition in the label renderer.

The renderer draws a list item's marker as a SEPARATE render element placed
left of the item's text at the hanging-indent column, and aligns a whole list
block as ONE unit (one flush marker column under every alignment). These tests
pin the composition, the group alignment, the pen split, and the no-marker
fast path.
"""

from __future__ import annotations

from plt_optimizer.generate.label_renderer import (
    _compose_marker_block,
    _marker_line_clone,
    _render_positioned_lines,
    measure_line_natural_width,
)
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine
from plt_optimizer.generate.text_geometry import LineSeg, Stroke, TextBlock


def _line(
    text: str = "item",
    *,
    marker: str = "",
    group: int = -1,
    indent: float = 0.0,
    alignment: str = "center",
    font: str = "dino",
    toolpath: float = 0.34,
) -> ResolvedTextLine:
    """Build a resolved line carrying the list metadata under test."""
    return ResolvedTextLine(
        text=text,
        nominal_text_height=0.4,
        toolpath_text_height=toolpath,
        cutter_diameter=0.06,
        character_spacing=0.09,
        line_spacing=0.1,
        text_h_alignment=alignment,
        font=font,
        marker_text=marker,
        align_group_id=group,
        list_item=bool(marker) or group >= 0,
        indent_left=indent,
    )


def _label(*lines: ResolvedTextLine, width: float = 6.0, h_margin: float = 0.2) -> ResolvedLabel:
    """Build a minimal resolved label carrying the given lines."""
    return ResolvedLabel(
        id="lbl",
        count=1,
        width=width,
        height=3.0,
        margin=h_margin,
        h_margin=h_margin,
        v_margin=0.2,
        content=list(lines),
    )


class TestMarkerLineClone:
    """The marker-only render clone."""

    def test_text_becomes_the_marker(self) -> None:
        """The clone renders the marker's own text."""
        line = _line("alpha", marker="-")
        clone = _marker_line_clone(line)
        assert clone.text == "-"
        assert clone.plain_text == "-"

    def test_marker_metadata_is_cleared(self) -> None:
        """The clone is not itself a list line (single-pass composition)."""
        line = _line("alpha", marker="-", group=3, indent=0.5)
        clone = _marker_line_clone(line)
        assert clone.marker_text == ""
        assert clone.align_group_id == -1
        assert clone.list_item is False

    def test_typography_is_inherited(self) -> None:
        """The marker rides the item's font, height and cutter."""
        line = _line("alpha", marker="-", font="gotland")
        clone = _marker_line_clone(line)
        assert clone.font == "gotland"
        assert clone.toolpath_text_height == line.toolpath_text_height
        assert clone.cutter_diameter == line.cutter_diameter


class TestComposeMarkerBlock:
    """The pure block composition (partitions + placement)."""

    @staticmethod
    def _block(x: float, width: float) -> TextBlock:
        """Build a one-stroke horizontal block from ``x`` to ``x + width``."""
        stroke = Stroke(
            pen_up=complex(x, 0.0),
            segments=(LineSeg(complex(x, 0.0), complex(x + width, 0.0)),),
        )
        return TextBlock(strokes=(stroke,))

    def test_marker_stays_at_zero_text_at_the_indent(self) -> None:
        """The composed block places the text at the hanging-indent column."""
        composed, _glyphs, _words = _compose_marker_block(
            self._block(0.0, 0.1),
            ((0,),),
            None,
            self._block(0.0, 0.5),
            ((0,),),
            None,
            "-",
            0.3,
        )
        bounds = composed.bounds()
        assert bounds is not None
        assert bounds[0] == 0.0  # the marker's left edge anchors the line
        assert bounds[2] == 0.8  # 0.3 indent + 0.5 text width

    def test_strokes_concatenate_in_order(self) -> None:
        """The marker's strokes come first, then the text's."""
        composed, glyphs, _words = _compose_marker_block(
            self._block(0.0, 0.1),
            ((0,),),
            None,
            self._block(0.0, 0.5),
            ((0,),),
            None,
            "-",
            0.3,
        )
        assert len(composed.strokes) == 2
        # The text's glyph group is offset past the marker's strokes.
        assert glyphs == ((0,), (1,))

    def test_word_groups_concatenate_with_the_marker_word(self) -> None:
        """The marker owns its word group; the text's words follow it."""
        _composed, _glyphs, words = _compose_marker_block(
            self._block(0.0, 0.1),
            ((0,),),
            None,
            self._block(0.0, 0.5),
            ((0,),),
            [("alpha", [0])],
            "-",
            0.3,
        )
        assert words is not None
        assert words[0][0] == "-"
        assert words[1] == ("alpha", [1])

    def test_line_mode_word_groups_stay_none(self) -> None:
        """A line-mode render (no word groups anywhere) composes to None."""
        _composed, _glyphs, words = _compose_marker_block(
            self._block(0.0, 0.1),
            ((0,),),
            None,
            self._block(0.0, 0.5),
            ((0,),),
            None,
            "-",
            0.3,
        )
        assert words is None


class TestPositionedMarkerLines:
    """The full positioning pass over list lines (real fonts)."""

    def test_plain_line_renders_unchanged(self) -> None:
        """A line with no marker keeps the historical single-render path."""
        label = _label(_line("ALPHA"))
        positioned = _render_positioned_lines(label)
        assert len(positioned) == 1
        _index, block, entry, _words, _glyphs = positioned[0]
        assert entry.line_text == "ALPHA"
        assert block.bounds() is not None

    def test_marker_line_extends_the_block_left(self) -> None:
        """The composed line's ink spans marker column + text."""
        plain = _render_positioned_lines(_label(_line("AAA")))[0][1]
        marked = _render_positioned_lines(_label(_line("AAA", marker="-", group=0, indent=0.3)))[0][
            1
        ]
        plain_bounds = plain.bounds()
        marked_bounds = marked.bounds()
        assert plain_bounds is not None and marked_bounds is not None
        # The marker + gap widen the line by (approximately) the indent.
        assert (marked_bounds[2] - marked_bounds[0]) > (plain_bounds[2] - plain_bounds[0])

    def test_display_text_names_the_composed_line(self) -> None:
        """The entry reports 'marker text' (the relief's character map)."""
        label = _label(_line("AAA", marker="-", group=0, indent=0.3))
        _index, _block, entry, _words, glyphs = _render_positioned_lines(label)[0]
        assert entry.line_text == "- AAA"
        # The partition covers the marker's glyph plus the text's glyphs.
        assert len(glyphs) == len("-AAA".replace(" ", ""))

    def test_group_shares_one_left_edge_under_center_alignment(self) -> None:
        """A list block centers as one unit: every line shares its left edge."""
        label = _label(
            _line("AAA", marker="-", group=0, indent=0.3, alignment="center"),
            _line("BBBBBB", marker="-", group=0, indent=0.3, alignment="center"),
        )
        bounds = [positioned[1].bounds() for positioned in _render_positioned_lines(label)]
        assert bounds[0] is not None and bounds[1] is not None
        assert bounds[0][0] == bounds[1][0]

    def test_continuation_line_hangs_at_the_text_column(self) -> None:
        """A marker-less group line starts at the group's left + its indent."""
        label = _label(
            _line("AAA", marker="-", group=0, indent=0.3, alignment="left"),
            _line("continuation text", group=0, indent=0.3, alignment="left"),
        )
        bounds = [positioned[1].bounds() for positioned in _render_positioned_lines(label)]
        assert bounds[0] is not None and bounds[1] is not None
        assert bounds[1][0] - bounds[0][0] == 0.3

    def test_left_alignment_anchors_the_marker_column(self) -> None:
        """A left-aligned list starts its markers at the margin."""
        label = _label(_line("AAA", marker="-", group=0, indent=0.3, alignment="left"))
        positioned = _render_positioned_lines(label)
        bounds = positioned[0][1].bounds()
        assert bounds is not None
        assert bounds[0] == 0.2  # the label's h_margin

    def test_right_alignment_anchors_the_widest_line(self) -> None:
        """A right-aligned list's widest line ends at the right margin."""
        label = _label(
            _line("AAA", marker="-", group=0, indent=0.3, alignment="right"),
            _line("BBBBBBBB", marker="-", group=0, indent=0.3, alignment="right"),
            width=6.0,
            h_margin=0.2,
        )
        bounds = [positioned[1].bounds() for positioned in _render_positioned_lines(label)]
        assert bounds[0] is not None and bounds[1] is not None
        widest = max(bounds[0][2], bounds[1][2])
        assert widest == 5.8  # width - h_margin

    def test_indent_shrinks_the_continuation_budget(self) -> None:
        """A continuation line over the narrowed budget compresses.

        The budget is ``inner_width - 2 * h_margin - indent``, so the deep
        indent forces the margin-compression scale below 1.0.
        """
        line = ResolvedTextLine(
            text="CONTINUATION TEXT THAT IS FAR TOO WIDE FOR THE NARROW BUDGET",
            nominal_text_height=0.4,
            toolpath_text_height=0.34,
            cutter_diameter=0.06,
            character_spacing=0.09,
            line_spacing=0.1,
            max_h_compress=0.5,
            text_h_alignment="left",
            font="dino",
            align_group_id=0,
            list_item=True,
            indent_left=2.5,
        )
        label = _label(line, width=6.0, h_margin=0.2)
        _index, _block, entry, _words, _glyphs = _render_positioned_lines(label)[0]
        assert entry.compression_scale < 1.0

    def test_marker_measures_like_plain_text(self) -> None:
        """measure_line_natural_width on the clone gives the marker's width."""
        marker_line = _marker_line_clone(_line("alpha", marker="Step iv)"))
        width = measure_line_natural_width(marker_line)
        plain = measure_line_natural_width(_line("Step iv)"))
        assert width == plain
