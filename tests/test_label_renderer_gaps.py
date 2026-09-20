"""Coverage tests for defensive and post-processing branches in ``label_renderer``.

The main suites (``test_label_renderer.py``, ``test_collision_detection.py``,
``test_collision_resolution.py``) exercise the happy paths of the render
pipeline. This file drives the residual statements and partial branches that
only fire on degenerate or hand-crafted inputs:

- the out-of-range line-index guard in ``_collision_threshold``;
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

from collections.abc import Iterator
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pytest
import vpype as vp

from plt_optimizer.generate import label_renderer
from plt_optimizer.generate.label_renderer import (
    _center_text_layer_vertically,
    _collect_hpgl_geometry,
    _collision_threshold,
    _linecollection_to_hpgl,
    _render_boundary_local,
    _render_text_local_with_bounds,
    _transform_hpgl_coordinates,
    render_label_to_plt,
)
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine


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


class _BoundslessLineCollection(vp.LineCollection):
    """Non-empty collection whose ``bounds()`` reports ``None``.

    Trips the defensive ``bounds is None`` skip in the first pass of
    ``_render_positioned_lines`` (a real vpype collection cannot be both
    non-empty and bounds-less).
    """

    def bounds(self) -> Optional[tuple[float, float, float, float]]:
        """Report no bounds regardless of content."""
        return None


class _TranslateClearsLineCollection(vp.LineCollection):
    """Collection that loses all geometry when translated.

    Trips the ``positioned_bounds is not None`` guard in the second pass of
    ``_render_positioned_lines``: the line renders with valid bounds but the
    positioning translate empties it, so no collision entry is recorded.
    """

    def translate(self, dx: float, dy: float) -> None:
        """Translate, then drop every segment."""
        super().translate(dx, dy)
        self._lines.clear()


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
            assert _collision_threshold(label, line_index) == pytest.approx(expected)

        # Sanity: the in-range index uses the line's own 0.03in cutter.
        in_range = 0.5 * (0.015 + 0.03) + 0.15
        assert _collision_threshold(label, 0) == pytest.approx(in_range)


class TestRenderLabelOnceLayerGuards:
    """Branches 569->568 / 574->583: empty text pens and empty boundaries."""

    def test_empty_text_pen_layer_is_not_added(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A pen whose LineCollection is empty must not reach the document."""

        def fake_by_pen(
            label: ResolvedLabel,
            pen_map: Optional[dict[float, int]] = None,
        ) -> tuple[dict[int, vp.LineCollection], list[label_renderer._LineEntry]]:
            return {9: vp.LineCollection()}, []

        monkeypatch.setattr(label_renderer, "_render_text_lines_by_pen", fake_by_pen)
        rendered = render_label_to_plt(_label())

        assert "SP9" not in rendered.plt_content
        # The boundary layer still renders, keeping the footprint measurable.
        assert "SP2" in rendered.plt_content
        assert rendered.width == pytest.approx(3.0, abs=0.01)

    def test_empty_boundary_layer_is_not_added(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty boundary collection must not create an SP2 section."""
        monkeypatch.setattr(
            label_renderer, "_render_boundary_local", lambda label: vp.LineCollection()
        )
        rendered = render_label_to_plt(_label())

        assert "SP2" not in rendered.plt_content
        assert "SP1" in rendered.plt_content


class TestRenderLabelOnceTerminator:
    """Branch 591->598 / line 595: PLT terminator normalization variants."""

    @staticmethod
    def _patch_export(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
        """Replace the export step so it writes ``content`` verbatim."""

        def fake_export(
            doc: vp.Document,
            output_path: Path,
            label: ResolvedLabel,
            text_pens: Optional[set[int]] = None,
        ) -> None:
            Path(output_path).write_text(content, encoding="utf-8")

        monkeypatch.setattr(label_renderer, "_export_to_plt_with_postprocessing", fake_export)

    def test_content_already_terminated_by_percent_is_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Content ending in ``%`` must not gain a second terminator."""
        self._patch_export(monkeypatch, "IN;DF;PS0;SP1;PU0,0;PD1000,1000;SP0;IN;%")
        rendered = render_label_to_plt(_label(content=[]))

        assert rendered.plt_content.endswith("%")
        assert rendered.plt_content.count("%") == 1
        assert rendered.width == pytest.approx(1.0, abs=1e-9)

    def test_content_without_in_footer_gets_terminator_appended(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Content ending in a bare ``;`` must be closed with ``;%``."""
        self._patch_export(monkeypatch, "IN;DF;PS0;SP1;PU0,0;PD1000,1000;")
        rendered = render_label_to_plt(_label(content=[]))

        assert rendered.plt_content == "IN;DF;PS0;SP1;PU0,0;PD1000,1000;%"


class TestCollectHpglGeometryArcGuard:
    """Branch 653->660: an ``AA`` command with fewer than three parameters."""

    def test_short_arc_command_is_skipped(self) -> None:
        """``AA`` needs (cx, cy, angle); two-parameter arcs are ignored."""
        points, arcs = _collect_hpgl_geometry("PU100,200;AA300,400;SP0;")

        assert points == [(100, 200)]
        assert arcs == []

    def test_well_formed_arc_still_parses(self) -> None:
        """A full ``AA`` command still yields one radius-bearing arc."""
        _points, arcs = _collect_hpgl_geometry("PU400,200;AA300,200,90;")

        assert arcs == [(300, 200, 100)]


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

        assert out == "IN;DF;PS0;SP1;PU1000,1000;PD2000,2000;SP0;IN;"

    def test_point_less_segment_is_skipped(self) -> None:
        """A segment with a length but no iterable points emits nothing."""
        lc = vp.LineCollection()
        lc.append(np.array([1 + 1j, 2 + 2j]))
        lc._lines.append(_EmptyIterableSegment())
        doc = vp.Document()
        doc.layers[1] = lc

        out = _linecollection_to_hpgl(doc)

        assert out == "IN;DF;PS0;SP1;PU1000,1000;PD2000,2000;SP0;IN;"

    def test_single_point_segment_emits_pen_up_only(self) -> None:
        """A lone vertex produces ``PU`` with no trailing ``PD``."""
        lc = vp.LineCollection()
        lc.append(np.array([1 + 1j, 2 + 2j]))
        # append() filters degenerate arrays, so inject the lone vertex raw.
        lc._lines.append(np.array([0.5 + 0.5j]))
        doc = vp.Document()
        doc.layers[1] = lc

        out = _linecollection_to_hpgl(doc)

        assert "PU500,500" in out
        assert "PD500,500" not in out

    def test_empty_document_has_no_footer(self) -> None:
        """A document with no content keeps the header-only output."""
        out = _linecollection_to_hpgl(vp.Document())

        assert out == "IN;DF;PS0;"


class TestCenterTextLayerDefensive:
    """Malformed tokens and the already-centered early return."""

    @staticmethod
    def _label_for_centering() -> ResolvedLabel:
        """Label whose expected text center is y=500 plotter units."""
        return ResolvedLabel(id="centering", count=1, width=2.0, height=1.0, margin=0.1)

    def test_malformed_coordinate_tokens_are_survivable(self, tmp_path: Path) -> None:
        """Unparsable Y tokens are skipped in detection and left intact.

        Covers both the ``_section_y_coords`` guard (the bad token must not
        poison the measured text bounds) and the ``adjust_coords_in_pen``
        guard (the bad token must be rewritten verbatim).
        """
        plt_file = tmp_path / "label.plt"
        plt_file.write_text(
            "IN;DF;PS0;SP1;PU100,200;PD900,300;PA500,-;SP0;IN;%",
            encoding="utf-8",
        )

        _center_text_layer_vertically(plt_file, self._label_for_centering())

        content = plt_file.read_text(encoding="utf-8")
        # Valid ys [200, 300] center at 250; expected 500 -> +250 shift.
        assert "PU100,450" in content
        assert "PD900,550" in content
        # The malformed token was returned unchanged by the adjuster.
        assert "PA500,-" in content

    def test_already_centered_text_is_left_untouched(self, tmp_path: Path) -> None:
        """A text block centered at the expected Y needs no adjustment."""
        plt_file = tmp_path / "label.plt"
        original = "IN;DF;PS0;SP1;PU0,400;PD1000,600;SP0;IN;%"
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

    def test_line_without_bounds_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-empty render reporting no bounds is dropped safely."""

        def factory() -> vp.LineCollection:
            stub = _BoundslessLineCollection()
            stub.append(np.array([0 + 0j, 1 + 0.3j]))
            return stub

        monkeypatch.setattr(label_renderer, "render_text_line_ftext", _stub_ftext(factory))
        _lc, entries = _render_text_local_with_bounds(_label())

        assert entries == []

    def test_line_losing_geometry_on_translate_is_not_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A line emptied by the positioning translate gets no entry."""

        def factory() -> vp.LineCollection:
            stub = _TranslateClearsLineCollection()
            stub.append(np.array([0 + 0j, 1 + 0.3j]))
            return stub

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
