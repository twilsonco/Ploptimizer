"""Coverage tests for defensive and post-processing branches in ``label_renderer``.

The main suites (``test_label_renderer.py``, ``test_collision_detection.py``,
``test_collision_resolution.py``) exercise the happy paths of the render
pipeline. This file drives the residual statements and partial branches that
only fire on degenerate or hand-crafted inputs:

- the out-of-range line-index guard in ``collision_threshold``;
- empty text-pen / empty boundary guards in ``_render_label_once``;
- the PLT terminator normalization branches (already ``%`` vs. bare ``;``);
- malformed ``AA`` arcs and un-flipped transforms in the HPGL helpers;
- dirty layers/segments in ``_linecollection_to_hpgl`` (``None`` layers,
  zero-length segments, point-less segments, single-point segments, and a
  document with no content at all);
- malformed coordinate tokens and the already-centered early return in
  ``_center_text_layer_vertically``;
- unrenderable text lines (empty text, bounds-less renders, geometry lost on
  translate) in ``_render_positioned_lines``;
- the ``vp.rect`` shape guards in ``_render_boundary_local``.

Tests only monkeypatch module-level collaborators or build degenerate
vpype/numpy objects directly -- production source is never modified.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pytest
import vpype as vp

from plt_optimizer.core.units import HPGL_UNITS_PER_INCH
from plt_optimizer.generate import font_registry, label_renderer
from plt_optimizer.generate.collision.detection import collision_threshold
from plt_optimizer.generate.label_renderer import (
    _center_text_layer_vertically,
    _collect_hpgl_geometry,
    _linecollection_to_hpgl,
    _render_boundary_local,
    _render_line_block,
    _render_text_local_with_bounds,
    _transform_hpgl_coordinates,
    render_label_to_plt,
)
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine
from plt_optimizer.generate.text_geometry import LineSeg, Stroke, TextBlock

# Whole-plotter-scale in integer device units (1 inch), for hand-built HPGL
# fixtures and the emission expectations of the HPGL writer helpers.
U = int(HPGL_UNITS_PER_INCH)


def _make_line(text: str, height: float = 0.3) -> ResolvedTextLine:
    """Build a cutter-compensated ``ResolvedTextLine`` for local rendering."""
    cutter_dia = 0.03
    return ResolvedTextLine(
        text=text,
        nominal_text_height=height,
        toolpath_text_height=height - cutter_dia,
        cutter_diameter=cutter_dia,
        character_spacing=0.0,
        line_spacing=0.0,
    )


def _label(
    content: Optional[list[ResolvedTextLine]] = None,
    width: float = 3.0,
    height: float = 1.0,
    margin: float = 0.1,
    hole_text_collision_distance: Optional[float] = None,
    hole_cutter_diameter: Optional[float] = None,
) -> ResolvedLabel:
    """Build a minimal ``ResolvedLabel`` (single "HELLO" line by default)."""
    if content is None:
        content = [_make_line("HELLO")]
    kwargs: dict[str, float] = {}
    if hole_text_collision_distance is not None:
        kwargs["hole_text_collision_distance"] = hole_text_collision_distance
    if hole_cutter_diameter is not None:
        kwargs["hole_cutter_diameter"] = hole_cutter_diameter
    return ResolvedLabel(
        id="gap_label",
        count=1,
        width=width,
        height=height,
        margin=margin,
        h_margin=margin,
        v_margin=margin,
        holes=[],
        content=content,
        **kwargs,
    )


class _EmptyIterableSegment:
    """Segment-like object with a length but no iterable points.

    Trips the ``if not points`` guard in ``_linecollection_to_hpgl``: the
    object passes the ``len(segment) == 0`` check but yields no coordinates.
    """

    def __len__(self) -> int:
        """Report a non-zero length so the empty-segment guard is skipped."""
        return 1

    def __iter__(self) -> Iterator[complex]:
        """Yield no points at all."""
        return iter(())


def _stub_ftext(factory: Callable[[], vp.LineCollection]) -> Callable[..., vp.LineCollection]:
    """Build a ``render_text_line_ftext`` replacement returning fresh stubs.

    Args:
        factory: Callable producing one stub collection per rendered line.

    Returns:
        A keyword-compatible callable ignoring text/height arguments.
    """

    def _fake(*args: object, **kwargs: object) -> vp.LineCollection:
        return factory()

    return _fake


class TestCollisionThresholdDefensiveIndex:
    """Line 133: out-of-range line indices assume no text stroke."""

    def test_out_of_range_index_assumes_zero_text_cutter(self) -> None:
        """Negative and past-the-end indices fall back to a 0.0in cutter."""
        label = _label(
            hole_text_collision_distance=0.15,
            hole_cutter_diameter=0.015,
        )
        expected = 0.5 * (0.015 + 0.0) + 0.15
        for line_index in (-1, len(label.content)):
            assert collision_threshold(label, line_index) == pytest.approx(expected)

        # Sanity: the in-range index uses the line's own 0.03in cutter.
        in_range = 0.5 * (0.015 + 0.03) + 0.15
        assert collision_threshold(label, 0) == pytest.approx(in_range)


class TestRenderLabelOnceLayerGuards:
    """Branches 569->568 / 574->583: empty text pens and empty boundaries."""

    def test_empty_text_pen_layer_is_not_added(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A pen whose LineCollection is empty must not reach the document."""

        def fake_by_pen(
            label: ResolvedLabel,
            pen_map: Optional[dict[tuple[float, str], int]] = None,
            chunk_mode: label_renderer.TextChunkMode = label_renderer.TextChunkMode.LINE,
            **kwargs: object,
        ) -> tuple[
            dict[int, tuple[TextBlock, ...]],
            list[label_renderer._LineEntry],
            list[label_renderer.TextChunkRecord],
        ]:
            return {9: ()}, [], []

        monkeypatch.setattr(label_renderer, "_render_text_lines_by_pen", fake_by_pen)
        rendered = render_label_to_plt(_label())

        assert "SP9" not in rendered.plt_content
        # The boundary layer still renders, keeping the footprint measurable.
        assert "SP2" in rendered.plt_content
        assert rendered.width == pytest.approx(3.0, abs=0.01)

    def test_empty_boundary_layer_is_not_added(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty boundary must not create an SP2 section."""
        monkeypatch.setattr(label_renderer, "_render_boundary_hpgl", lambda label: "")
        rendered = render_label_to_plt(_label())

        assert "SP2" not in rendered.plt_content
        assert "SP1" in rendered.plt_content


class TestRenderLabelOnceTerminator:
    """render_label_to_plt takes the exported content verbatim (stripped).

    The historical ``%``-terminator normalization was removed with the move
    to EngraveLab framing (content ends with the bare ``SP;`` footer), so
    the only post-read step is whitespace stripping.
    """

    @staticmethod
    def _patch_export(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
        """Replace the export step so it writes ``content`` verbatim."""

        def fake_export(
            doc: vp.Document,
            output_path: Path,
            label: ResolvedLabel,
            text_pens: Optional[set[int]] = None,
            text_blocks: Optional[dict[int, tuple[TextBlock, ...]]] = None,
            band_center_offset_inches: float = 0.0,
            boundary_hpgl: str = "",
            holes_hpgl: str = "",
        ) -> None:
            Path(output_path).write_text(content, encoding="utf-8")

        monkeypatch.setattr(label_renderer, "_export_to_plt_with_postprocessing", fake_export)

    def test_sp_footer_is_kept_without_percent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A bare ``SP;`` footer is kept verbatim; no ``%`` is appended."""
        self._patch_export(monkeypatch, f"IN;PA;SP1;PU0,0;PD{U},{U};SP;")
        rendered = render_label_to_plt(_label(content=[]))

        assert rendered.plt_content == f"IN;PA;SP1;PU0,0;PD{U},{U};SP;"
        assert "%" not in rendered.plt_content
        assert rendered.width == pytest.approx(1.0, abs=1e-9)

    def test_surrounding_whitespace_is_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Leading/trailing whitespace is stripped, content otherwise kept."""
        self._patch_export(monkeypatch, f"\n  IN;PA;SP1;PU0,0;PD{U},{U};SP;  \n")
        rendered = render_label_to_plt(_label(content=[]))

        assert rendered.plt_content == f"IN;PA;SP1;PU0,0;PD{U},{U};SP;"


class TestCollectHpglGeometryArcGuard:
    """Branch 653->660: an ``AA`` command with fewer than three parameters."""

    def test_short_arc_command_is_skipped(self) -> None:
        """``AA`` needs (cx, cy, angle); two-parameter arcs are ignored."""
        points, arcs = _collect_hpgl_geometry("PU100,200;AA300,400;SP0;")

        assert points == [(100, 200)]
        assert arcs == []

    def test_well_formed_arc_still_parses(self) -> None:
        """A full ``AA`` command yields one radius-bearing arc extent."""
        _points, arcs = _collect_hpgl_geometry("PU400,200;AA300,200,90;")

        assert len(arcs) == 1
        arc = arcs[0]
        assert (arc.cx, arc.cy, arc.radius) == (300, 200, 100)
        # Pen at (400,200) sits due east of the center (300,200).
        assert arc.start_angle == pytest.approx(0.0)
        assert arc.sweep_angle == pytest.approx(90.0)


class TestTransformWithoutFlip:
    """Branch 703->705: ``_map_y`` without a ``flip_y_span``."""

    def test_scale_and_translate_without_mirroring(self) -> None:
        """Y passes through untouched by mirroring when no span is given."""
        transformed = _transform_hpgl_coordinates(
            "PU100,200;PD300,400;", scale_x=2.0, translate_x=10
        )

        assert transformed == "PU210,200;PD610,400;"


class TestLinecollectionToHpglDefensive:
    """Dirty document layers and segments in ``_linecollection_to_hpgl``."""

    def test_none_and_empty_layers_are_skipped(self) -> None:
        """Layers that are ``None`` or empty emit no pen section."""
        doc = vp.Document()
        good = vp.LineCollection()
        good.append(np.array([1 + 1j, 2 + 2j]))
        doc.layers[1] = good
        doc.layers[3] = None
        doc.layers[4] = vp.LineCollection()

        out = _linecollection_to_hpgl(doc)

        assert "SP1" in out
        assert "SP3" not in out
        assert "SP4" not in out

    def test_none_and_zero_length_segments_are_skipped(self) -> None:
        """``None`` and empty-array segments never emit HPGL commands."""
        lc = vp.LineCollection()
        lc.append(np.array([1 + 1j, 2 + 2j]))
        # Force the collection into a state vpype's public API never produces.
        lc._lines.append(None)
        lc._lines.append(np.array([]))
        doc = vp.Document()
        doc.layers[1] = lc

        out = _linecollection_to_hpgl(doc)

        assert out == f"IN;PA;SP1;PU{U},{U};PD{2 * U},{2 * U};SP;"

    def test_point_less_segment_is_skipped(self) -> None:
        """A segment with a length but no iterable points emits nothing."""
        lc = vp.LineCollection()
        lc.append(np.array([1 + 1j, 2 + 2j]))
        lc._lines.append(_EmptyIterableSegment())
        doc = vp.Document()
        doc.layers[1] = lc

        out = _linecollection_to_hpgl(doc)

        assert out == f"IN;PA;SP1;PU{U},{U};PD{2 * U},{2 * U};SP;"

    def test_single_point_segment_emits_pen_up_only(self) -> None:
        """A lone vertex produces ``PU`` with no trailing ``PD``."""
        lc = vp.LineCollection()
        lc.append(np.array([1 + 1j, 2 + 2j]))
        # append() filters degenerate arrays, so inject the lone vertex raw.
        lc._lines.append(np.array([0.5 + 0.5j]))
        doc = vp.Document()
        doc.layers[1] = lc

        out = _linecollection_to_hpgl(doc)

        assert f"PU{U // 2},{U // 2}" in out
        assert f"PD{U // 2},{U // 2}" not in out

    def test_empty_document_has_no_footer(self) -> None:
        """A document with no content keeps the header-only output."""
        out = _linecollection_to_hpgl(vp.Document())

        assert out == "IN;PA;"


class TestLinecollectionToHpglBlockPens:
    """Arc-native ``text_blocks`` layers in ``_linecollection_to_hpgl``."""

    @staticmethod
    def _block(*stroke_specs: bool) -> TextBlock:
        """Build a block with one stroke per flag (True = has geometry).

        Args:
            stroke_specs: One flag per stroke; ``True`` produces a one-segment
                stroke, ``False`` a segment-less (empty) stroke.

        Returns:
            A TextBlock for emission tests.
        """
        strokes = tuple(
            Stroke(
                pen_up=complex(1.0 + index, 1.0),
                segments=(LineSeg(complex(1.0 + index, 1.0), complex(2.0 + index, 2.0)),)
                if has_geometry
                else (),
            )
            for index, has_geometry in enumerate(stroke_specs)
        )
        return TextBlock(strokes=strokes)

    def test_all_empty_block_pen_is_skipped(self) -> None:
        """A block pen whose strokes are all empty emits no SP section."""
        doc = vp.Document()
        good = vp.LineCollection()
        good.append(np.array([1 + 1j, 2 + 2j]))
        doc.layers[2] = good

        out = _linecollection_to_hpgl(doc, text_blocks={4: (self._block(False),)})

        assert "SP4" not in out
        assert "SP2" in out

    def test_empty_strokes_within_block_pen_are_skipped(self) -> None:
        """Segment-less strokes inside a live block pen emit nothing."""
        out = _linecollection_to_hpgl(vp.Document(), text_blocks={1: (self._block(False, True),)})

        assert out == f"IN;PA;SP1;PU{2 * U},{U};PD{3 * U},{2 * U};SP;"

    def test_block_pen_emits_without_document_layers(self) -> None:
        """A block-only document still gets header, pen section and footer."""
        out = _linecollection_to_hpgl(vp.Document(), text_blocks={1: (self._block(True),)})

        assert out == f"IN;PA;SP1;PU{U},{U};PD{2 * U},{2 * U};SP;"


class TestRenderLineBlockWordFallback:
    """TTF word groups fall back to whole-line chunking on 1:1 mismatch."""

    def test_dropped_contour_disables_word_groups(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A contour lost in adaptation voids the contour-indexed groups."""
        # Two contours, one of which is a lone vertex: block_from_linecollection
        # drops it, so strokes (1) != contours (2) and groups cannot be trusted.
        lc = vp.LineCollection()
        lc.append(np.array([0 + 0j, 1 + 1j]))
        # append() filters degenerate arrays, so inject the lone vertex raw.
        lc._lines.append(np.array([2 + 2j]))

        def fake_with_words(
            *args: object, **kwargs: object
        ) -> tuple[vp.LineCollection, list[tuple[str, list[int]]]]:
            return lc, [("AB", [0]), ("C", [1])]

        monkeypatch.setattr(label_renderer, "render_text_line_ftext_with_words", fake_with_words)
        line = ResolvedTextLine(
            text="AB C",
            nominal_text_height=0.3,
            toolpath_text_height=0.27,
            cutter_diameter=0.03,
            character_spacing=0.0,
            line_spacing=0.0,
        )
        block, groups, glyph_groups = _render_line_block(line, label_renderer.TextChunkMode.WORD)
        assert groups is None
        # The 1:1 contour/stroke guard voids the glyph partition too.
        assert glyph_groups == ()
        assert len(block.strokes) == 1

    def test_line_mode_dropped_contour_disables_glyph_groups(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LINE mode: a contour lost in adaptation voids the glyph partition."""
        lc = vp.LineCollection()
        lc.append(np.array([0 + 0j, 1 + 1j]))
        # append() filters degenerate arrays, so inject the lone vertex raw.
        lc._lines.append(np.array([2 + 2j]))

        def fake_ftext(*args: object, **kwargs: object) -> vp.LineCollection:
            return lc

        monkeypatch.setattr(label_renderer, "render_text_line_ftext", fake_ftext)
        line = ResolvedTextLine(
            text="AB",
            nominal_text_height=0.3,
            toolpath_text_height=0.27,
            cutter_diameter=0.03,
            character_spacing=0.0,
            line_spacing=0.0,
        )
        block, groups, glyph_groups = _render_line_block(line, label_renderer.TextChunkMode.LINE)
        assert groups is None
        assert glyph_groups == ()
        assert len(block.strokes) == 1


class TestSmallCapsDispatch:
    """``_render_line_block`` forwards the small-caps scale only for ``SC`` fonts."""

    def test_ttf_variant_forwards_small_caps_scale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A TTF ``SC`` line passes ``small_caps_scale`` to the ftext path."""
        calls: dict[str, object] = {}

        def fake_ftext(*args: object, **kwargs: object) -> vp.LineCollection:
            calls.update(kwargs)
            lc = vp.LineCollection()
            lc.append(np.array([0 + 0j, 1 + 1j]))
            return lc

        monkeypatch.setattr(label_renderer, "render_text_line_ftext", fake_ftext)
        line = ResolvedTextLine(
            text="Ab",
            nominal_text_height=0.3,
            toolpath_text_height=0.27,
            cutter_diameter=0.03,
            character_spacing=0.0,
            line_spacing=0.0,
            font=f"{font_registry.DEFAULT_FONT_NAME} SC",
            sc_scale=0.45,
        )
        _render_line_block(line, label_renderer.TextChunkMode.LINE)
        assert calls["small_caps_scale"] == pytest.approx(0.45)

    def test_ttf_base_font_omits_small_caps_scale(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A plain TTF line never receives the parameter (legacy call shape)."""
        calls: dict[str, object] = {}

        def fake_ftext(*args: object, **kwargs: object) -> vp.LineCollection:
            calls.update(kwargs)
            lc = vp.LineCollection()
            lc.append(np.array([0 + 0j, 1 + 1j]))
            return lc

        monkeypatch.setattr(label_renderer, "render_text_line_ftext", fake_ftext)
        line = ResolvedTextLine(
            text="Ab",
            nominal_text_height=0.3,
            toolpath_text_height=0.27,
            cutter_diameter=0.03,
            character_spacing=0.0,
            line_spacing=0.0,
            sc_scale=0.45,
        )
        _render_line_block(line, label_renderer.TextChunkMode.LINE)
        assert "small_caps_scale" not in calls


def _markup_line(text: str, **kwargs: object) -> ResolvedTextLine:
    """Build a resolved line with its markup parsed (delimiters ``<>``)."""
    from plt_optimizer.generate.markup import parse_markup

    styled = parse_markup(text, "<", ">")
    line = ResolvedTextLine(
        text=text,
        nominal_text_height=0.5,
        toolpath_text_height=0.44,
        cutter_diameter=0.06,
        character_spacing=0.0,
        line_spacing=0.0,
        **kwargs,  # type: ignore[arg-type]
    )
    return dataclasses.replace(line, plain_text=styled.plain_text, styled_runs=styled.runs)


class TestInlineSmallCapsGate:
    """``_small_caps_render_gate``: the single decision the renderers + probe share."""

    def test_markup_free_line_is_inert(self) -> None:
        """A tag-free line returns ``None`` (the historical call shape)."""
        line = _markup_line("Hello")
        ref = font_registry.resolve_font("Dino")
        assert label_renderer._small_caps_render_gate(line, ref) is None

    def test_italic_only_line_is_inert(self) -> None:
        """Markup without a small-caps run never engages the transform."""
        line = _markup_line("<i>Hello</i>", font="Dino")
        ref = font_registry.resolve_font("Dino")
        assert label_renderer._small_caps_render_gate(line, ref) is None

    def test_small_caps_run_selects_scale_and_flags(self) -> None:
        """A ``<sc>`` run yields the cascaded ``sc_scale`` + per-char flags."""
        line = _markup_line("a<sc>b</sc>", font="Dino", sc_scale=0.42)
        ref = font_registry.resolve_font("Dino")
        gate = label_renderer._small_caps_render_gate(line, ref)
        assert gate is not None
        scale, flags = gate
        assert scale == pytest.approx(0.42)
        assert flags == (False, True)

    def test_inert_on_a_small_caps_font(self) -> None:
        """A `` SC`` line is already small caps: the tag has no additional effect."""
        line = _markup_line("<sc>abc</sc>", font="Dino SC")
        ref = font_registry.resolve_font("Dino SC")
        assert label_renderer._small_caps_render_gate(line, ref) is None

    def test_inert_on_a_symbol_font(self) -> None:
        """A symbol font (catalog symbol_font) has no lowercase: the tag is a no-op."""
        symbol = sorted(
            name for name, data in font_registry.load_plt_fonts().items() if data.symbol_font
        )
        if not symbol:  # pragma: no cover - the shipped catalog has Wingdings
            pytest.skip("no symbol fonts in the catalog")
        name = symbol[0].capitalize()
        line = _markup_line("<sc>abc</sc>", font=name)
        ref = font_registry.resolve_font(name)
        assert label_renderer._small_caps_render_gate(line, ref) is None


class TestEngravedBaselineRaise:
    """``_baseline_shift_inches`` composes the engraved-baseline alignment raise.

    A character engraving with cutter ``d`` other than the line's ``cutter``
    rides ``(d - cutter) / 2`` so its ENGRAVED baseline matches the plain run's
    (and, for ``<b>``, its engraved capline too). The raise rides the renderers'
    existing ``baseline_shifts`` array, composing with the ``<sup>``/``<sub>``
    shift; a line whose markup engraves with one tool stays on the historical
    call shape (``None``).
    """

    def test_markup_free_line_has_no_shift(self) -> None:
        """A tag-free line keeps the historical call shape (``None``)."""
        assert label_renderer._baseline_shift_inches(_markup_line("Hello")) is None

    def test_bold_run_shifts_by_the_half_decrease(self) -> None:
        """A ``<b>`` run's shift is (bold - plain) / 2; the plain chars stay 0."""
        line = _markup_line("a<b>B</b>c", bold_cutter_diameter=0.09)
        shifts = label_renderer._baseline_shift_inches(line)
        assert shifts is not None
        assert shifts == (0.0, 0.015, 0.0)

    def test_bold_render_kwargs_carry_the_shift(self) -> None:
        """A PLT bold line's kwargs pair the geometry plan with the raise."""
        line = _markup_line("a<b>B</b>c", bold_cutter_diameter=0.09)
        kwargs = label_renderer._glyph_render_kwargs(line, plt=True)
        assert kwargs["baseline_shifts"] == (0.0, 0.015, 0.0)
        assert "glyph_scales" in kwargs and "cutter_diameters" in kwargs

    def test_ttf_render_kwargs_carry_the_shift(self) -> None:
        """The TTF path receives the same raise (PLT parity, no cutters)."""
        line = _markup_line("a<b>B</b>c", bold_cutter_diameter=0.09)
        kwargs = label_renderer._glyph_render_kwargs(line, plt=False)
        assert kwargs["baseline_shifts"] == (0.0, 0.015, 0.0)
        assert "cutter_diameters" not in kwargs

    def test_top_of_inventory_bold_stays_on_the_fast_path(self) -> None:
        """No bold rung (top of inventory): no scale, no raise, empty kwargs."""
        line = _markup_line("<b>AB</b>")
        assert label_renderer._glyph_render_kwargs(line, plt=False) == {}

    def test_shift_composes_with_the_sup_sub_shift(self) -> None:
        """A bold ``<sup>`` character rides the sup shift PLUS the raise."""
        line = _markup_line("a<sup><b>B</b></sup>", bold_cutter_diameter=0.09)
        shifts = label_renderer._baseline_shift_inches(line)
        assert shifts is not None
        # toolpath cap 0.44: sup shift 0.75 * 0.44 = 0.33, plus the run's
        # engraved-floor raise (run cutter - line cutter) / 2. The bare sup
        # downscales, so its run takes the smaller ladder cutter (a drop).
        raise_in = (line.per_char_cutters[1] - line.cutter_diameter) / 2
        assert raise_in < 0.0
        assert shifts[0] == pytest.approx(0.0)
        assert shifts[1] == pytest.approx(0.75 * 0.44 + raise_in)


class TestInlineSmallCapsDispatch:
    """``_render_line_block`` forwards the per-character selection to the renderers."""

    def test_plt_line_forwards_lowercase_scale_and_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A PLT ``<sc>`` line passes ``lowercase_scale`` + ``lowercase_flags``."""
        calls: dict[str, object] = {}

        def fake_render(*args: object, **kwargs: object) -> TextBlock:
            calls.update(kwargs)
            return TextBlock.empty()

        monkeypatch.setattr(label_renderer, "render_text_line_plt_font", fake_render)
        monkeypatch.setattr(
            label_renderer,
            "render_text_line_plt_font_with_glyphs",
            lambda *a, **k: (TextBlock.empty(), [], []),
        )
        line = _markup_line("a<sc>b</sc>", font="Dino", sc_scale=0.5)
        _render_line_block(line, label_renderer.TextChunkMode.LINE)
        assert calls["lowercase_scale"] == pytest.approx(0.5)
        assert calls["lowercase_flags"] == (False, True)

    def test_ttf_line_forwards_small_caps_scale_and_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A TTF ``<sc>`` line passes ``small_caps_scale`` + ``small_caps_flags``."""
        calls: dict[str, object] = {}

        def fake_ftext(*args: object, **kwargs: object) -> vp.LineCollection:
            calls.update(kwargs)
            lc = vp.LineCollection()
            lc.append(np.array([0 + 0j, 1 + 1j]))
            return lc

        monkeypatch.setattr(label_renderer, "render_text_line_ftext", fake_ftext)
        line = _markup_line("a<sc>b</sc>", font=font_registry.DEFAULT_FONT_NAME, sc_scale=0.5)
        _render_line_block(line, label_renderer.TextChunkMode.LINE)
        assert calls["small_caps_scale"] == pytest.approx(0.5)
        assert calls["small_caps_flags"] == (False, True)

    def test_sc_font_keeps_the_font_level_kwargs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On a `` SC`` font the tag adds no flags (bit-identical to the variant)."""
        calls: dict[str, object] = {}

        def fake_ftext(*args: object, **kwargs: object) -> vp.LineCollection:
            calls.update(kwargs)
            lc = vp.LineCollection()
            lc.append(np.array([0 + 0j, 1 + 1j]))
            return lc

        monkeypatch.setattr(label_renderer, "render_text_line_ftext", fake_ftext)
        line = _markup_line(
            "<sc>abc</sc>", font=f"{font_registry.DEFAULT_FONT_NAME} SC", sc_scale=0.45
        )
        _render_line_block(line, label_renderer.TextChunkMode.LINE)
        assert calls["small_caps_scale"] == pytest.approx(0.45)
        assert "small_caps_flags" not in calls

    def test_markup_free_line_keeps_the_historical_call_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tag-free line receives no small-caps kwargs at all."""
        calls: dict[str, object] = {}

        def fake_ftext(*args: object, **kwargs: object) -> vp.LineCollection:
            calls.update(kwargs)
            lc = vp.LineCollection()
            lc.append(np.array([0 + 0j, 1 + 1j]))
            return lc

        monkeypatch.setattr(label_renderer, "render_text_line_ftext", fake_ftext)
        line = _markup_line("abc", font=font_registry.DEFAULT_FONT_NAME, sc_scale=0.45)
        _render_line_block(line, label_renderer.TextChunkMode.LINE)
        assert "small_caps_scale" not in calls
        assert "small_caps_flags" not in calls


class TestValidatedGlyphGroups:
    """The partition guard: usable only on an exact 1:1 stroke cover."""

    def test_missing_pairs_yield_empty(self) -> None:
        """``None``/empty pairs mean no partition."""
        assert label_renderer._validated_glyph_groups(None, 3) == ()
        assert label_renderer._validated_glyph_groups([], 3) == ()

    def test_exact_cover_is_accepted(self) -> None:
        """A disjoint full cover converts to index tuples in text order."""
        pairs = [("A", [0, 1]), ("B", [2])]
        assert label_renderer._validated_glyph_groups(pairs, 3) == ((0, 1), (2,))

    def test_duplicate_index_voids_the_partition(self) -> None:
        """A stroke claimed twice is not an exact cover."""
        pairs = [("A", [0]), ("B", [0, 1])]
        assert label_renderer._validated_glyph_groups(pairs, 2) == ()

    def test_missing_index_voids_the_partition(self) -> None:
        """A stroke no group claims is not an exact cover."""
        pairs = [("A", [0])]
        assert label_renderer._validated_glyph_groups(pairs, 2) == ()


class TestWordLocalGlyphGroups:
    """Line-level glyph groups re-expressed in one word's stroke frame."""

    def test_indices_remap_into_the_word_frame(self) -> None:
        """A glyph's line indices map onto the word's local positions."""
        glyph_groups = ((0, 1), (2, 3), (4,))
        word_indices = [2, 3]
        assert label_renderer._word_local_glyph_groups(glyph_groups, word_indices) == ((0, 1),)

    def test_empty_partition_stays_empty(self) -> None:
        """No line-level groups -> no word-level groups."""
        assert label_renderer._word_local_glyph_groups((), [0, 1]) == ()


class TestCenterTextLayerDefensive:
    """Malformed tokens and the already-centered early return."""

    @staticmethod
    def _label_for_centering() -> ResolvedLabel:
        """Label whose expected text center is y=U/2 plotter units."""
        return ResolvedLabel(
            id="centering", count=1, width=2.0, height=1.0, margin=0.1, h_margin=0.1, v_margin=0.1
        )

    def test_malformed_coordinate_tokens_are_survivable(self, tmp_path: Path) -> None:
        """Unparsable Y tokens are skipped in detection and left intact.

        Covers both the ``_section_y_coords`` guard (the bad token must not
        poison the measured text bounds) and the ``adjust_coords_in_pen``
        guard (the bad token must be rewritten verbatim).
        """
        plt_file = tmp_path / "label.plt"
        plt_file.write_text(
            "IN;PA;SP1;PU100,200;PD900,300;PA500,-;SP;",
            encoding="utf-8",
        )

        _center_text_layer_vertically(plt_file, self._label_for_centering())

        content = plt_file.read_text(encoding="utf-8")
        # Valid ys [200, 300] center at 250; expected U/2 -> +(U/2 - 250) shift.
        shift = U // 2 - 250
        assert f"PU100,{200 + shift}" in content
        assert f"PD900,{300 + shift}" in content
        # The malformed token was returned unchanged by the adjuster.
        assert "PA500,-" in content

    def test_already_centered_text_is_left_untouched(self, tmp_path: Path) -> None:
        """A text block centered at the expected Y needs no adjustment."""
        plt_file = tmp_path / "label.plt"
        lo, hi = U // 2 - 100, U // 2 + 100
        original = f"IN;PA;SP1;PU0,{lo};PD1000,{hi};SP;"
        plt_file.write_text(original, encoding="utf-8")

        _center_text_layer_vertically(plt_file, self._label_for_centering())

        assert plt_file.read_text(encoding="utf-8") == original


class TestRenderPositionedLinesSkipGuards:
    """Lines 1284/1288/1314 and branch 1389->1400 in the shared render core."""

    def test_empty_text_line_is_skipped_but_indices_survive(self) -> None:
        """An empty text line renders nothing and keeps later indices intact."""
        label = _label(content=[_make_line(""), _make_line("REAL")])

        _lc, entries = _render_text_local_with_bounds(label)

        assert [entry[0] for entry in entries] == [1]

    def test_all_lines_unrenderable_returns_nothing(self) -> None:
        """A label whose every line is empty yields no geometry at all."""
        label = _label(content=[_make_line(""), _make_line("")])

        text_lc, entries = _render_text_local_with_bounds(label)

        assert text_lc.is_empty()
        assert entries == []

    def test_line_without_geometry_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A render whose polylines all carry no segments is dropped safely.

        Blocks measure bounds analytically (no bounds <=> no segments), so
        the historical "bounds-less non-empty collection" guard folded into
        the emptiness check when rendering moved to arc-native blocks: a
        lone vertex adapts to a segment-less stroke and the line renders
        nothing.
        """

        def factory() -> vp.LineCollection:
            stub = vp.LineCollection()
            # append() filters degenerate arrays, so inject the lone vertex raw.
            stub._lines.append(np.array([0.5 + 0.5j]))
            return stub

        monkeypatch.setattr(label_renderer, "render_text_line_ftext", _stub_ftext(factory))
        _lc, entries = _render_text_local_with_bounds(_label())

        assert entries == []

    def test_empty_rendered_line_is_not_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A line that renders to a fully empty block gets no entry."""

        def factory() -> vp.LineCollection:
            return vp.LineCollection()

        monkeypatch.setattr(label_renderer, "render_text_line_ftext", _stub_ftext(factory))
        text_lc, entries = _render_text_local_with_bounds(_label())

        assert entries == []
        assert text_lc.is_empty()


class TestRenderBoundaryLocalGuards:
    """Branch 1475->1477: unexpected ``vp.rect`` return shapes."""

    def test_empty_rect_array_produces_empty_collection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zero-sized rect array must not be appended."""
        monkeypatch.setattr(label_renderer.vp, "rect", lambda *a, **k: np.array([]))

        assert _render_boundary_local(_label()).is_empty()

    def test_non_array_rect_produces_empty_collection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-ndarray rect result must not be appended."""
        monkeypatch.setattr(label_renderer.vp, "rect", lambda *a, **k: [1 + 2j])

        assert _render_boundary_local(_label()).is_empty()


def _stroke_block(x_min: float, x_max: float, y_top: float = 0.4) -> TextBlock:
    """Build a single-stroke rectangular block spanning [x_min, x_max]."""
    y_bottom = 0.0
    points = [
        complex(x_min, y_bottom),
        complex(x_max, y_bottom),
        complex(x_max, y_top),
        complex(x_min, y_top),
        complex(x_min, y_bottom),
    ]
    segments = [LineSeg(points[i], points[i + 1]) for i in range(len(points) - 1)]
    return TextBlock(strokes=(Stroke(pen_up=points[0], segments=tuple(segments)),))


class TestFontNamesGate:
    """``_font_names_kwargs``: the all-PLT gate for the per-character font array."""

    def test_markup_free_line_is_inert(self) -> None:
        """A tag-free line returns {} (the historical call shape)."""
        assert label_renderer._font_names_kwargs(_markup_line("Hello")) == {}

    def test_all_plt_line_selects_the_font_array(self) -> None:
        """A PLT base + PLT run yields the per-character font tuple."""
        line = _markup_line("a<f:Wingdings>b</f>", font="Dino")
        kwargs = label_renderer._font_names_kwargs(line)
        assert kwargs == {"font_names": (None, "Wingdings")}

    def test_ttf_base_line_is_inert(self) -> None:
        """A TTF base font composes segments instead (the walk renders PLT only)."""
        line = _markup_line("a<f:Wingdings>b</f>", font=font_registry.DEFAULT_FONT_NAME)
        assert label_renderer._font_names_kwargs(line) == {}

    def test_ttf_run_font_is_inert(self) -> None:
        """A TTF run font composes segments (the walk cannot render TTF)."""
        line = _markup_line("a<f:" + font_registry.DEFAULT_FONT_NAME + ">b</f>", font="Dino")
        assert label_renderer._font_names_kwargs(line) == {}

    def test_unknown_font_is_inert_for_the_gate(self) -> None:
        """An unresolvable font name returns {} (the render surfaces the error)."""
        line = _markup_line("a<f:NoSuchFont>b</f>", font="Dino")
        assert label_renderer._font_names_kwargs(line) == {}


class TestWalkSmallCapsGate:
    """``_walk_small_caps_kwargs``: the <f:Base+SC> run-font small caps."""

    def test_markup_free_line_is_inert(self) -> None:
        """A tag-free line returns {} (the historical call shape)."""
        ref = font_registry.resolve_font("Dino")
        assert label_renderer._walk_small_caps_kwargs(_markup_line("a"), ref, None) == {}

    def test_plain_run_font_is_inert(self) -> None:
        """A non-variant run font adds no small-caps selection."""
        ref = font_registry.resolve_font("Dino")
        line = _markup_line("a<f:Wingdings>b</f>", font="Dino")
        assert label_renderer._walk_small_caps_kwargs(line, ref, None) == {}

    def test_sc_variant_run_font_selects_its_characters(self) -> None:
        """``<f:Dino+SC>`` flags exactly the run's characters."""
        ref = font_registry.resolve_font("Dino")
        line = _markup_line("a<f:Dino+SC>bc</f>d", font="Dino", sc_scale=0.7)
        kwargs = label_renderer._walk_small_caps_kwargs(line, ref, None)
        assert kwargs["lowercase_scale"] == pytest.approx(0.7)
        assert kwargs["lowercase_flags"] == (False, True, True, False)

    def test_inert_on_a_small_caps_base_font(self) -> None:
        """A SC base font owns the whole-line transform (the variant is inert)."""
        ref = font_registry.resolve_font("Dino SC")
        line = _markup_line("a<f:Dino+SC>b</f>", font="Dino SC")
        assert label_renderer._walk_small_caps_kwargs(line, ref, None) == {}

    def test_symbol_font_run_is_excluded(self) -> None:
        """A symbol-font run never shrinks (no lowercase to shrink)."""
        ref = font_registry.resolve_font("Dino")
        # The inline <sc> gate flags all 4 chars; the Wingdings run's 2 are
        # excluded, so the flags differ from the gate's -> the merge fires.
        line = _markup_line("a<f:Wingdings>bc</f>d", font="Dino", sc_scale=0.7)
        gate = (0.7, (True, True, True, True))
        kwargs = label_renderer._walk_small_caps_kwargs(line, ref, gate)
        assert kwargs["lowercase_flags"] == (True, False, False, True)


class TestFontNamesDispatch:
    """``_render_line_block`` forwards font runs to the walk / composes segments."""

    def test_plt_line_forwards_font_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An all-PLT font-run line passes ``font_names`` to the walk."""
        calls: dict[str, object] = {}

        def fake_render(*args: object, **kwargs: object) -> TextBlock:
            calls.update(kwargs)
            return TextBlock.empty()

        monkeypatch.setattr(label_renderer, "render_text_line_plt_font", fake_render)
        monkeypatch.setattr(
            label_renderer,
            "render_text_line_plt_font_with_glyphs",
            lambda *a, **k: (TextBlock.empty(), [], []),
        )
        line = _markup_line("a<f:Wingdings>b</f>", font="Dino")
        _render_line_block(line, label_renderer.TextChunkMode.LINE)
        assert calls["font_names"] == (None, "Wingdings")

    def test_markup_free_line_keeps_the_historical_call_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tag-free line receives no font_names kwarg at all."""
        calls: dict[str, object] = {}

        def fake_render(*args: object, **kwargs: object) -> TextBlock:
            calls.update(kwargs)
            return TextBlock.empty()

        monkeypatch.setattr(label_renderer, "render_text_line_plt_font", fake_render)
        monkeypatch.setattr(
            label_renderer,
            "render_text_line_plt_font_with_glyphs",
            lambda *a, **k: (TextBlock.empty(), [], []),
        )
        line = _markup_line("abc", font="Dino")
        _render_line_block(line, label_renderer.TextChunkMode.LINE)
        assert "font_names" not in calls


class TestEnforceCapLineDispatch:
    """``_render_line_core`` threads the cap-line clamp to both renderer families."""

    def test_plt_line_forwards_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An enabled PLT line passes ``enforce_cap_line=True`` to the walk."""
        calls: dict[str, object] = {}

        def fake_render(*args: object, **kwargs: object) -> TextBlock:
            calls.update(kwargs)
            return TextBlock.empty()

        monkeypatch.setattr(label_renderer, "render_text_line_plt_font", fake_render)
        monkeypatch.setattr(
            label_renderer,
            "render_text_line_plt_font_with_glyphs",
            lambda *a, **k: (TextBlock.empty(), [], []),
        )
        line = _markup_line("abc", font="Dino", enforce_cap_line=True)
        _render_line_block(line, label_renderer.TextChunkMode.LINE)
        assert calls["enforce_cap_line"] is True

    def test_disabled_plt_line_keeps_the_historical_call_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An opted-out PLT line receives no enforce kwarg at all."""
        calls: dict[str, object] = {}

        def fake_render(*args: object, **kwargs: object) -> TextBlock:
            calls.update(kwargs)
            return TextBlock.empty()

        monkeypatch.setattr(label_renderer, "render_text_line_plt_font", fake_render)
        monkeypatch.setattr(
            label_renderer,
            "render_text_line_plt_font_with_glyphs",
            lambda *a, **k: (TextBlock.empty(), [], []),
        )
        line = _markup_line("abc", font="Dino", enforce_cap_line=False)
        _render_line_block(line, label_renderer.TextChunkMode.LINE)
        assert "enforce_cap_line" not in calls

    def test_ttf_line_forwards_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An enabled TTF line passes ``cap_line_enforced=True`` to ftext."""
        calls: dict[str, object] = {}

        def fake_ftext(text: str, **kwargs: object) -> vp.LineCollection:
            calls.update(kwargs)
            lc = vp.LineCollection()
            lc.append(np.array([0 + 0j, 0.5 + 0.4j], dtype=complex))
            return lc

        monkeypatch.setattr(label_renderer, "render_text_line_ftext", fake_ftext)
        monkeypatch.setattr(label_renderer, "glyph_groups_for_line", lambda *a, **k: [("x", [0])])
        line = _markup_line("abc", font=font_registry.DEFAULT_FONT_NAME, enforce_cap_line=True)
        _render_line_block(line, label_renderer.TextChunkMode.LINE)
        assert calls["cap_line_enforced"] is True

    def test_disabled_ttf_line_keeps_the_historical_call_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An opted-out TTF line receives no enforcement kwarg at all."""
        calls: dict[str, object] = {}

        def fake_ftext(text: str, **kwargs: object) -> vp.LineCollection:
            calls.update(kwargs)
            lc = vp.LineCollection()
            lc.append(np.array([0 + 0j, 0.5 + 0.4j], dtype=complex))
            return lc

        monkeypatch.setattr(label_renderer, "render_text_line_ftext", fake_ftext)
        monkeypatch.setattr(label_renderer, "glyph_groups_for_line", lambda *a, **k: [("x", [0])])
        line = _markup_line("abc", font=font_registry.DEFAULT_FONT_NAME, enforce_cap_line=False)
        _render_line_block(line, label_renderer.TextChunkMode.LINE)
        assert "cap_line_enforced" not in calls

    def test_font_segment_clone_carries_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cross-family segment clone inherits the parent's enforcement."""
        seen: dict[str, bool] = {}

        def fake_plt(text: str, **kwargs: object) -> TextBlock:
            seen["plt"] = bool(kwargs.get("enforce_cap_line", False))
            return _stroke_block(0.0, 1.0)

        def fake_ftext(text: str, **kwargs: object) -> vp.LineCollection:
            seen["ttf"] = bool(kwargs.get("cap_line_enforced", False))
            lc = vp.LineCollection()
            lc.append(np.array([0 + 0j, 0.5 + 0.4j], dtype=complex))
            return lc

        monkeypatch.setattr(label_renderer, "render_text_line_plt_font", fake_plt)
        monkeypatch.setattr(label_renderer, "render_text_line_ftext", fake_ftext)
        monkeypatch.setattr(
            label_renderer,
            "render_text_line_plt_font_with_glyphs",
            lambda *a, **k: (_stroke_block(0.0, 1.0), [], [("x", [0])]),
        )
        monkeypatch.setattr(label_renderer, "glyph_groups_for_line", lambda *a, **k: [("x", [0])])
        line = _markup_line(
            "a <f:" + font_registry.DEFAULT_FONT_NAME + ">b</f>",
            font="Dino",
            scale_cutter_inventory=(0.06,),
            enforce_cap_line=True,
        )
        _render_line_block(line, label_renderer.TextChunkMode.LINE)
        assert seen == {"plt": True, "ttf": True}


class TestFontSegmentComposition:
    """``_render_line_block_composed``: cross-family segments on a running cursor."""

    def test_segments_compose_on_the_cursor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each segment lands after the previous ink's right edge + clearance.

        A PLT base with a TTF run: the fake renders return fixed-width blocks,
        so the composed bounds pin the bbox + stroke-floor boundary advance
        (0.5 * (cutter_L + cutter_R) + character_spacing + kerning_min_gap_fraction).
        """
        blocks = {"plt": _stroke_block(0.0, 1.0), "ttf": _stroke_block(0.0, 0.5)}

        def fake_plt(text: str, **kwargs: object) -> TextBlock:
            return blocks["plt"]

        def fake_ftext(text: str, **kwargs: object) -> vp.LineCollection:
            lc = vp.LineCollection()
            # The TTF block adapts from the LineCollection; reuse the PLT box
            # via the adapted path by emitting the same rectangle.
            lc.append(np.array([0 + 0j, 0.5 + 0j, 0.5 + 0.4j, 0 + 0.4j, 0 + 0j], dtype=complex))
            return lc

        monkeypatch.setattr(label_renderer, "render_text_line_plt_font", fake_plt)
        monkeypatch.setattr(label_renderer, "render_text_line_ftext", fake_ftext)
        monkeypatch.setattr(
            label_renderer,
            "render_text_line_plt_font_with_glyphs",
            lambda *a, **k: (blocks["plt"], [], [("x", [0])]),
        )
        monkeypatch.setattr(
            label_renderer,
            "glyph_groups_for_line",
            lambda *a, **k: [("x", [0])],
        )
        line = _markup_line(
            "a<f:" + font_registry.DEFAULT_FONT_NAME + ">b</f>",
            font="Dino",
            scale_cutter_inventory=(0.06,),
        )
        block, _words, glyphs = _render_line_block(line, label_renderer.TextChunkMode.LINE)
        bounds = block.bounds()
        assert bounds is not None
        # Segment 1: the Dino 'a' at x = 0..1. Segment 2 (the TTF run) lands at
        # the previous ink's right edge + the engraved-stroke floor
        # 0.5 * (0.06 + 0.06) = 0.06 (no space between the segments, and
        # character_spacing / kerning_min_gap_fraction are 0 on this line).
        expected_x = 1.0 + 0.06
        assert bounds[0] == pytest.approx(0.0)
        assert bounds[2] == pytest.approx(expected_x + 0.5, abs=1e-9)
        # One glyph group per non-space character, exact cover.
        assert len(glyphs) == 2
        flat = [i for group in glyphs for i in group]
        assert sorted(flat) == list(range(len(block.strokes)))

    def test_space_only_segment_advances_by_the_space_width(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A space between segments breaks kerning and advances by the space."""
        calls: list[str] = []

        def fake_plt(text: str, **kwargs: object) -> TextBlock:
            calls.append(text)
            return _stroke_block(0.0, 1.0)

        def fake_ftext(text: str, **kwargs: object) -> vp.LineCollection:
            calls.append(text)
            lc = vp.LineCollection()
            lc.append(np.array([0 + 0j, 0.5 + 0j, 0.5 + 0.4j, 0 + 0.4j, 0 + 0j], dtype=complex))
            return lc

        monkeypatch.setattr(label_renderer, "render_text_line_plt_font", fake_plt)
        monkeypatch.setattr(label_renderer, "render_text_line_ftext", fake_ftext)
        monkeypatch.setattr(
            label_renderer,
            "render_text_line_plt_font_with_glyphs",
            lambda *a, **k: (_stroke_block(0.0, 1.0), [], [("x", [0])]),
        )
        monkeypatch.setattr(label_renderer, "glyph_groups_for_line", lambda *a, **k: [("x", [0])])
        line = _markup_line(
            "a <f:" + font_registry.DEFAULT_FONT_NAME + ">b</f>",
            font="Dino",
            scale_cutter_inventory=(0.06,),
        )
        _render_line_block(line, label_renderer.TextChunkMode.LINE)
        # The space is its own (skipped) segment: the renderers see 'a' and 'b'.
        assert calls == ["a", "b"]


class TestInlineCutterDispatch:
    """``<c:D>`` routes the run's strokes onto its own tool's pen."""

    def test_cutter_run_forwards_its_diameter(self) -> None:
        """The PLT path receives the run's explicit tool in ``cutter_diameters``."""
        line = _markup_line("a<c:0.03>b</c>c", scale_cutter_inventory=(0.03, 0.06))
        kwargs = label_renderer._glyph_render_kwargs(line, plt=True)
        assert kwargs["cutter_diameters"] == (0.06, 0.03, 0.06)

    def test_cutter_run_forwards_its_geometry_scale(self) -> None:
        """The run's silhouette is ``nominal - D`` (the <b> centerline rule)."""
        line = _markup_line("a<c:0.03>b</c>c", scale_cutter_inventory=(0.03, 0.06))
        kwargs = label_renderer._glyph_render_kwargs(line, plt=True)
        expected = (0.5 - 0.03) / (0.5 - 0.06)
        assert kwargs["glyph_scales"][1] == pytest.approx(expected)
        assert kwargs["glyph_scales"][0] == pytest.approx(1.0)

    def test_cutter_run_carries_the_baseline_raise(self) -> None:
        """The run rides the engraved-baseline raise (d - cutter) / 2."""
        line = _markup_line("a<c:0.03>b</c>c", scale_cutter_inventory=(0.03, 0.06))
        shifts = label_renderer._baseline_shift_inches(line)
        assert shifts is not None
        assert shifts[1] == pytest.approx((0.03 - 0.06) / 2)
        assert shifts[0] == pytest.approx(0.0)

    def test_ttf_path_forwards_the_geometry_scale(self) -> None:
        """The TTF path receives the run's scale (PLT parity, no diameters)."""
        line = _markup_line("a<c:0.03>b</c>c", scale_cutter_inventory=(0.03, 0.06))
        kwargs = label_renderer._glyph_render_kwargs(line, plt=False)
        assert "cutter_diameters" not in kwargs
        expected = (0.5 - 0.03) / (0.5 - 0.06)
        assert kwargs["glyph_scales"][1] == pytest.approx(expected)

    def test_stroke_cutter_map_splits_the_line(self) -> None:
        """The per-stroke cutter array splits the line across two tools."""
        from plt_optimizer.generate.label_renderer import (
            TextChunkMode,
            _render_line_block,
            _stroke_cutter_map,
        )

        line = _markup_line("a<c:0.03>b</c>c", scale_cutter_inventory=(0.03, 0.06))
        block, _words, glyphs = _render_line_block(line, TextChunkMode.LINE)
        cutters = _stroke_cutter_map(line, block, glyphs)
        # Two tools engrave the line, so the array is not empty and carries
        # both diameters.
        assert set(cutters) == {0.03, 0.06}

    def test_underline_run_splits_at_the_cutter_boundary(self) -> None:
        """One underline cannot span two tools: the run splits at the boundary."""
        from plt_optimizer.generate.label_renderer import _underline_render_gate

        line = _markup_line("a<u>b<c:0.03>c</c></u>", scale_cutter_inventory=(0.03, 0.06))
        runs = _underline_render_gate(line)
        # The underlined span covers the plain 'b' (0.06) and the overridden
        # 'c' (0.03), so it splits into two runs.
        assert len(runs) == 2
