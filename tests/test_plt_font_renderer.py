"""Tests for the arc-native PLT-extracted font renderer.

The bundled ``Fonts/plt_fonts.json`` provides the real glyph library; a
synthetic library (``tmp_path``) pins the exact layout math (advance,
gaps, normalization) with hand-computable glyph geometry.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from plt_optimizer.generate import font_registry, label_renderer, plt_font_renderer
from plt_optimizer.generate.label_renderer import (
    LabelRenderError,
    RenderedLabel,
    TextChunkMode,
    _render_line_block,
    render_label_to_plt,
)
from plt_optimizer.generate.plt_font_renderer import (
    GAP_HEIGHT_FRACTION,
    SPACE_HEIGHT_FRACTION,
    PltFontRenderError,
    clear_glyph_cache,
    render_text_line_plt_font,
    render_text_line_plt_font_with_words,
)
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine
from plt_optimizer.generate.text_geometry import ArcSeg, LineSeg, Stroke, TextBlock


@pytest.fixture(autouse=True)
def _clear_caches() -> object:
    """Isolate the font/registry/glyph lru_caches between tests."""
    font_registry.load_plt_fonts.cache_clear()
    font_registry.available_ttf_fonts.cache_clear()
    clear_glyph_cache()
    yield
    font_registry.load_plt_fonts.cache_clear()
    font_registry.available_ttf_fonts.cache_clear()
    clear_glyph_cache()


def _write_library(root: Path, glyphs: dict[str, str]) -> Path:
    """Write a synthetic ``plt_fonts.json`` holding one ``Test`` font.

    Args:
        root: Directory to write the library into.
        glyphs: Character -> HPGL mapping for the ``Test`` font.

    Returns:
        The library path (usable as the renderer's ``json_path``).
    """
    library = root / "plt_fonts.json"
    library.write_text(json.dumps({"Test": glyphs}), encoding="utf-8")
    return library


# Hand-computable glyph set (plotter units, 1000/in, device frame +y down):
#   A: diagonal (1.0 x 1.0 in), polyline-only.
#   B: vertical line at its left edge (0.0 wide, 1.0 tall).
#   C: full circle radius 0.5 (1.0 x 1.0 in), one native AA arc (the
#      parser opens arcs from a PD, matching the extracted font files).
#   x: short diagonal (0.5 x 0.5 in) for relative-size checks.
_SYNTH_GLYPHS = {
    "A": "PU500,500;PD-500,-500;",
    "B": "PU-500,-500;PD-500,500;",
    "C": "PU0,0;PD500,0;AA0,0,360;",
    "x": "PU250,250;PD-250,-250;",
}


@pytest.fixture
def synth_lib(tmp_path: Path) -> Path:
    """A synthetic font library path (font name ``Test``)."""
    return _write_library(tmp_path, _SYNTH_GLYPHS)


def _render(text: str, height: float, lib: Path, font: str = "Test", **kwargs: object) -> TextBlock:
    """Render ``text`` from the synthetic ``Test`` font."""
    return render_text_line_plt_font(text, height, font, json_path=lib, **kwargs)  # type: ignore[arg-type]


def _count_arcs(block: TextBlock) -> int:
    """Count arc segments in a rendered block."""
    return sum(1 for stroke in block.strokes for seg in stroke.segments if isinstance(seg, ArcSeg))


def _count_lines(block: TextBlock) -> int:
    """Count line segments in a rendered block."""
    return sum(1 for stroke in block.strokes for seg in stroke.segments if isinstance(seg, LineSeg))


class TestBundledLibraryRendering:
    """The shipped plt_fonts.json renders through the arc-native path."""

    def test_renders_upright_nonempty(self) -> None:
        """A real glyph string renders with the exact target height."""
        block = render_text_line_plt_font("DINO ARC", 0.5, "dino", cutter_diameter=0.04)
        bounds = block.bounds()
        assert bounds is not None
        min_x, min_y, max_x, max_y = bounds
        assert math.isclose(max_y - min_y, 0.5, rel_tol=1e-9)
        assert math.isclose(min_x, 0.0, abs_tol=1e-9)
        assert math.isclose(min_y, 0.0, abs_tol=1e-9)
        assert max_x > min_x

    def test_arcs_survive_end_to_end(self) -> None:
        """Arc-bearing glyphs keep native AA geometry (no flattening)."""
        block = render_text_line_plt_font("OO", 0.5, "Dino", cutter_diameter=0.04)
        assert _count_arcs(block) > 0
        hpgl = block.to_hpgl()
        assert "AA" in hpgl
        # Every arc keeps decimal-precision sweeps (huge-radius best-fit arcs).
        arc_tokens = [t for t in hpgl.split(";") if t.startswith("AA")]
        assert arc_tokens
        assert any("." in t for t in arc_tokens)

    def test_case_insensitive_font_name(self) -> None:
        """``dino``/``DINO`` resolve to the canonical ``Dino`` library."""
        lower = render_text_line_plt_font("AB", 0.4, "dino")
        upper = render_text_line_plt_font("AB", 0.4, "DINO")
        assert lower.bounds() == pytest.approx(upper.bounds())


class TestLayoutMath:
    """Advance, gap and normalization math against synthetic glyphs."""

    def test_inter_glyph_gap_formula(self, synth_lib: Path) -> None:
        """Neighbouring glyph edges sit cutter + 0.125h + char_spacing apart."""
        height = 1.0
        cutter = 0.04
        spacing = 0.02
        block = _render("AB", height, synth_lib, cutter_diameter=cutter, character_spacing=spacing)
        bounds = block.bounds()
        assert bounds is not None
        # A spans [0, 1.0]; the gap then B (zero-width) at the right edge.
        expected_width = 1.0 + cutter + GAP_HEIGHT_FRACTION * height + spacing
        assert bounds[2] == pytest.approx(expected_width, abs=1e-9)

    def test_space_advance_formula(self, synth_lib: Path) -> None:
        """A space contributes its own 0.5h advance (no inter-glyph gap)."""
        height = 1.0
        block = _render("A A", height, synth_lib, cutter_diameter=0.0, character_spacing=0.0)
        bounds = block.bounds()
        assert bounds is not None
        # A(1.0) + space(0.5) + A(1.0): the inter-glyph gap applies only
        # between adjacent glyphs WITHIN a word, never across a space.
        expected = 1.0 + SPACE_HEIGHT_FRACTION + 1.0
        assert bounds[2] == pytest.approx(expected, abs=1e-9)

    def test_whole_line_height_normalization(self, synth_lib: Path) -> None:
        """Union height normalizes to the target; short glyphs stay shorter."""
        block = _render("Ax", 0.8, synth_lib)
        bounds = block.bounds()
        assert bounds is not None
        assert bounds[3] - bounds[1] == pytest.approx(0.8, rel=1e-9)
        # 'x' is half the design height of 'A': its strokes span ~0.4in.
        x_strokes = block.strokes[1:]
        x_ys = [
            z.imag for stroke in x_strokes for seg in stroke.segments for z in (seg.start, seg.end)
        ]
        assert max(x_ys) - min(x_ys) == pytest.approx(0.4, rel=1e-6)

    def test_glyphs_land_on_origin_corner(self, synth_lib: Path) -> None:
        """The line's left edge sits at x=0 and its bottom at y=0."""
        block = _render("CB", 0.5, synth_lib)
        bounds = block.bounds()
        assert bounds is not None
        assert bounds[0] == pytest.approx(0.0, abs=1e-12)
        assert bounds[1] == pytest.approx(0.0, abs=1e-12)

    def test_arcs_measure_swept_extent(self, synth_lib: Path) -> None:
        """A glyph of native arcs contributes its swept box, not a chord box."""
        block = _render("C", 1.0, synth_lib)
        assert _count_arcs(block) == 1
        bounds = block.bounds()
        assert bounds is not None
        # Full-circle glyph: swept bounds == the circle's box (1.0 x 1.0).
        assert bounds[2] - bounds[0] == pytest.approx(1.0, abs=1e-9)
        assert bounds[3] - bounds[1] == pytest.approx(1.0, abs=1e-9)

    def test_mirror_negates_sweeps(self, synth_lib: Path) -> None:
        """The device-frame glyph mirrors to +y up with negated sweeps."""
        # Stored C sweeps +360 (device frame); the render frame negates it.
        block = _render("C", 1.0, synth_lib)
        arc = next(
            seg for stroke in block.strokes for seg in stroke.segments if isinstance(seg, ArcSeg)
        )
        assert arc.sweep_deg == pytest.approx(-360.0)


class TestWordGroups:
    """with_words partitions strokes exactly by construction."""

    def test_word_groups_partition_strokes(self, synth_lib: Path) -> None:
        """Word groups are contiguous stroke-index slices covering all strokes."""
        block, groups = render_text_line_plt_font_with_words(
            "A B A", 0.5, "Test", json_path=synth_lib
        )
        assert [word for word, _ in groups] == ["A", "B", "A"]
        all_indices = [i for _word, indices in groups for i in indices]
        assert sorted(all_indices) == list(range(len(block.strokes)))
        for _word, indices in groups:
            assert indices == sorted(indices)

    def test_blank_segment_yields_empty_group(self, synth_lib: Path) -> None:
        """A double space keeps column alignment with an empty group."""
        _block, groups = render_text_line_plt_font_with_words(
            "A  B", 0.5, "Test", json_path=synth_lib
        )
        assert [word for word, _ in groups] == ["A", "", "B"]
        assert groups[1][1] == []

    def test_empty_text(self, synth_lib: Path) -> None:
        """Empty input yields an empty block and no groups."""
        block, groups = render_text_line_plt_font_with_words("", 0.5, "Test", json_path=synth_lib)
        assert block.is_empty()
        assert groups == []

    def test_blank_only_text_keeps_word_structure(self, synth_lib: Path) -> None:
        """A spaces-only line renders nothing but reports its word slots."""
        block, groups = render_text_line_plt_font_with_words("  ", 0.5, "Test", json_path=synth_lib)
        assert block.is_empty()
        assert groups == [("", []), ("", []), ("", [])]


class TestCompressionInteraction:
    """Horizontal compression flattens arcs with a bounded chord error."""

    def test_compress_x_flattens_arcs_with_warning(
        self, synth_lib: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Compressing an arc-bearing line logs a WARNING and keeps polylines."""
        block = _render("C", 1.0, synth_lib)
        assert _count_arcs(block) == 1
        with caplog.at_level("WARNING"):
            compressed = block.compress_x(0.5, context="of label demo")
        assert _count_arcs(compressed) == 0
        assert _count_lines(compressed) > 1
        assert "flattened" in caplog.text
        assert "label demo" in caplog.text
        # The flattened chain stays within the compressed bounds.
        bounds = compressed.bounds()
        assert bounds is not None
        assert bounds[2] - bounds[0] == pytest.approx(0.5, abs=1e-9)


class TestErrorPaths:
    """Missing glyphs and unusable font names raise PltFontRenderError."""

    def test_missing_glyph_raises_with_char_and_font(self, synth_lib: Path) -> None:
        """A character absent from the font names the font and the character."""
        with pytest.raises(PltFontRenderError, match="has no glyph for character"):
            _render("A\u0100", 0.5, synth_lib)

    def test_unknown_font_raises_listing_choices(self, synth_lib: Path) -> None:
        """An unknown font name fails with the valid-name list."""
        with pytest.raises(PltFontRenderError, match="Unknown font"):
            _render("A", 0.5, synth_lib, font="Nope")

    def test_ttf_font_rejected_by_plt_renderer(self) -> None:
        """The PLT renderer refuses TrueType names (the TTF path owns them)."""
        with pytest.raises(PltFontRenderError, match="TrueType"):
            render_text_line_plt_font("A", 0.5, font_registry.DEFAULT_FONT_NAME)

    def test_missing_library_degrades_to_unknown_font(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing library means no PLT fonts: resolution reports unknown."""
        with caplog.at_level("WARNING"):
            with pytest.raises(PltFontRenderError, match="Unknown font"):
                render_text_line_plt_font("A", 0.5, "Dino", json_path=tmp_path / "absent.json")
        assert "not found" in caplog.text


class TestEmptyGlyphs:
    """Stored glyphs without geometry render as empty blocks."""

    def test_whitespace_glyph_string_is_empty(self, tmp_path: Path) -> None:
        """A glyph entry of only whitespace contributes no strokes."""
        lib = _write_library(tmp_path, {"A": "   ", "B": "PU-500,-500;PD-500,500;"})
        block = _render("AB", 1.0, lib)
        # Only B contributes; the line still normalizes to the target height.
        bounds = block.bounds()
        assert bounds is not None
        assert bounds[3] - bounds[1] == pytest.approx(1.0, abs=1e-9)
        assert len(block.strokes) == 1


class TestGlyphCache:
    """The glyph parse cache keys on the library path and clears cleanly."""

    def test_cache_isolates_libraries(self, tmp_path: Path) -> None:
        """Two libraries with different 'A' geometry render different widths."""
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        lib_a = _write_library(tmp_path / "a", {"A": "PU500,500;PD-500,-500;"})
        lib_b = _write_library(tmp_path / "b", {"A": "PU1500,500;PD-500,-500;"})
        narrow = _render("A", 1.0, lib_a)
        wide = _render("A", 1.0, lib_b)
        narrow_bounds = narrow.bounds()
        wide_bounds = wide.bounds()
        assert narrow_bounds is not None and wide_bounds is not None
        assert wide_bounds[2] > narrow_bounds[2]

    def test_clear_glyph_cache_picks_up_new_library(self, tmp_path: Path) -> None:
        """Editing a library in place is visible after clear_glyph_cache()."""
        lib = tmp_path / "plt_fonts.json"
        _write_library(tmp_path, {"A": "PU500,500;PD-500,-500;"})
        first = _render("A", 1.0, lib)
        _write_library(tmp_path, {"A": "PU1500,500;PD-500,-500;"})
        stale = _render("A", 1.0, lib)
        assert stale.bounds() == pytest.approx(first.bounds())
        clear_glyph_cache()
        font_registry.load_plt_fonts.cache_clear()
        fresh = _render("A", 1.0, lib)
        assert (fresh.bounds() or (0, 0, 0, 0))[2] > (first.bounds() or (0, 0, 0, 0))[2]


class TestModuleHygiene:
    """The renderer must stay importable without matplotlib."""

    def test_module_does_not_import_matplotlib(self) -> None:
        """No matplotlib import anywhere in the renderer's source."""
        source = Path(plt_font_renderer.__file__).read_text(encoding="utf-8")
        assert "import matplotlib" not in source


def _resolved_line(text: str, font: str, height: float = 0.3) -> ResolvedTextLine:
    """Build a cutter-compensated ``ResolvedTextLine`` in ``font``."""
    return ResolvedTextLine(
        text=text,
        nominal_text_height=height,
        toolpath_text_height=height - 0.03,
        cutter_diameter=0.03,
        character_spacing=0.02,
        line_spacing=0.0,
        font=font,
    )


def _triangle_block() -> TextBlock:
    """A one-stroke two-segment block for dispatch stubs."""
    return TextBlock(
        strokes=(Stroke(pen_up=0j, segments=(LineSeg(0j, 1 + 0.5j), LineSeg(1 + 0.5j, 2 + 0j))),)
    )


class TestLabelRendererDispatch:
    """``_render_line_block`` routes by the cascaded ``font`` field."""

    def test_plt_font_receives_canonical_name_and_metrics(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A PLT line calls the PLT renderer with the canonical font name."""
        calls: dict[str, object] = {}

        def fake_render(text: str, **kwargs: object) -> TextBlock:
            calls["text"] = text
            calls.update(kwargs)
            return _triangle_block()

        monkeypatch.setattr(label_renderer, "render_text_line_plt_font", fake_render)
        block, groups = _render_line_block(_resolved_line("AB", "dino"), TextChunkMode.LINE)
        assert calls["text"] == "AB"
        assert calls["font_name"] == "Dino"  # canonical, not the raw input
        assert calls["target_height_inches"] == pytest.approx(0.27)
        assert calls["cutter_diameter"] == pytest.approx(0.03)
        assert calls["character_spacing"] == pytest.approx(0.02)
        assert block.strokes == _triangle_block().strokes
        assert groups is None  # line mode never requests word groups

    def test_plt_word_mode_returns_groups(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """WORD chunk mode forwards the renderer's exact stroke groups."""

        def fake_with_words(
            text: str, **kwargs: object
        ) -> tuple[TextBlock, list[tuple[str, list[int]]]]:
            return _triangle_block(), [("AB", [0])]

        monkeypatch.setattr(label_renderer, "render_text_line_plt_font_with_words", fake_with_words)
        _block, groups = _render_line_block(_resolved_line("AB", "Dino"), TextChunkMode.WORD)
        assert groups == [("AB", [0])]

    def test_unknown_font_becomes_render_error(self) -> None:
        """An unresolvable cascaded font raises PltFontRenderError."""
        with pytest.raises(PltFontRenderError, match="Unknown font"):
            _render_line_block(_resolved_line("AB", "NoSuchFont"), TextChunkMode.LINE)

    def test_missing_glyph_aborts_label_render(self) -> None:
        """A glyph-less character fails the label with id + line index."""
        label = ResolvedLabel(
            id="font_fail",
            count=1,
            width=3.0,
            height=1.0,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            holes=[],
            content=[_resolved_line("A\u0100", "Dino")],
        )
        with pytest.raises(LabelRenderError, match=r"Label font_fail: text line 0") as excinfo:
            render_label_to_plt(label)
        assert "has no glyph for character" in str(excinfo.value)


class TestPltFontDemoExample:
    """Regression tests for tests_deps/plt_font_demo_job.yaml.

    The frozen fixture pins the ``font`` cascade (job -> label -> text line)
    and the arc-native contract: PLT-extracted fonts emit AA commands in the
    rendered label, TrueType fonts never do.
    """

    @staticmethod
    def _rendered() -> dict[str, tuple[ResolvedLabel, RenderedLabel]]:
        """Parse, resolve and render every label of the demo job."""
        from plt_optimizer.generate.resolution import resolve_job_spec
        from plt_optimizer.generate.schema import parse_yaml

        job = parse_yaml(Path("tests_deps/plt_font_demo_job.yaml"))
        return {rl.id: (rl, render_label_to_plt(rl)) for rl in resolve_job_spec(job)}

    def test_font_cascade_levels(self) -> None:
        """Job, label and line tiers each resolve to their canonical font."""
        rendered = self._rendered()
        dino_banner = rendered["dino_banner"][0]
        ttf_card = rendered["ttf_card"][0]
        mixed_tag = rendered["mixed_tag"][0]

        assert dino_banner.content[0].font == "Dino"  # job tier
        assert ttf_card.content[0].font == "ReliefSingleLineCAD-Regular"  # label tier
        assert mixed_tag.content[0].font == "Jhanuni"  # line tier
        assert mixed_tag.content[1].font == "Dino"  # falls through to job tier

    def test_plt_fonts_keep_native_arcs(self) -> None:
        """PLT-rendered labels contain AA commands; the TTF label has none."""
        rendered = self._rendered()
        assert "AA" in rendered["dino_banner"][1].plt_content
        assert "AA" in rendered["mixed_tag"][1].plt_content
        assert "AA" not in rendered["ttf_card"][1].plt_content

    def test_arc_counts_are_stable(self) -> None:
        """Arc counts per label are pinned (glyph parsing is deterministic)."""
        rendered = self._rendered()
        counts = {key: content.plt_content.count("AA") for key, (_rl, content) in rendered.items()}
        assert counts["dino_banner"] == 147
        assert counts["mixed_tag"] == 195
        assert counts["ttf_card"] == 0
