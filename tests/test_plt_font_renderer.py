"""Tests for the arc-native PLT-extracted font renderer (v2 schema).

The bundled ``Fonts/plt_fonts.json`` provides the real glyph library; a
synthetic v2 library (``tmp_path``) pins the exact layout math (baseline
anchoring, envelope kerning, space advance) with hand-computable glyph
geometry.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

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
    KERNING_WINDOW_FRACTION,
    MIN_GLYPH_WIDTH,
    SPACE_HEIGHT_FRACTION,
    PltFontRenderError,
    _GlyphGeometry,
    clear_glyph_cache,
    interpolate_envelope,
    kerning_offset,
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


def _v2_entry(
    hpgl: str,
    bbox: Optional[Tuple[float, float, float, float]] = None,
    left: Optional[Sequence[Tuple[float, float]]] = None,
    right: Optional[Sequence[Tuple[float, float]]] = None,
) -> Dict[str, Any]:
    """Build one v2 character entry (plotter units, baseline y=0, +y up).

    Args:
        hpgl: The glyph HPGL string.
        bbox: ``(min_x, min_y, max_x, max_y)`` stored bounding box.
        left: Left-envelope samples ``((x, y), ...)``.
        right: Right-envelope samples ``((x, y), ...)``.

    Returns:
        A JSON-serializable character entry.
    """
    entry: Dict[str, Any] = {"glyph": hpgl}
    if bbox is not None:
        entry["bounding_box"] = {
            "min_x": bbox[0],
            "min_y": bbox[1],
            "max_x": bbox[2],
            "max_y": bbox[3],
        }
    if left is not None:
        entry["left_envelope"] = [[x, y] for x, y in left]
    if right is not None:
        entry["right_envelope"] = [[x, y] for x, y in right]
    return entry


def _write_library(root: Path, characters: Dict[str, Dict[str, Any]]) -> Path:
    """Write a synthetic v2 ``plt_fonts.json`` holding one ``Test`` font.

    Args:
        root: Directory to write the library into.
        characters: Character -> v2 entry mapping for the ``Test`` font.

    Returns:
        The library path (usable as the renderer's ``json_path``).
    """
    library = root / "plt_fonts.json"
    library.write_text(
        json.dumps(
            {
                "Test": {
                    "reference_char": "A",
                    "declared_height_in": 1.0,
                    "reference_char_height_in": 1.0,
                    "normalized_ref_height": 1.0,
                    "characters": characters,
                }
            }
        ),
        encoding="utf-8",
    )
    return library


# Hand-computable v2 glyph set (plotter units, 1000/in, baseline y=0, +y up,
# left edge x=0):
#   A: diagonal (0,0)->(1000,1000) (1.0 x 1.0 in), polyline-only. Its left
#      and right silhouettes are both x = y (a single stroke).
#   B: zero-width vertical line on the baseline (0.0 x 1.0 in).
#   C: full circle radius 0.5 centered (500,500) (1.0 x 1.0 in), one native
#      AA arc (the parser opens arcs from a PD, matching extracted fonts).
#   x: short diagonal (0,0)->(500,500) for relative-size checks.
#   g: descender diagonal (0,-500)->(500,500) (hangs below the baseline).
_SYNTH_CHARACTERS: Dict[str, Dict[str, Any]] = {
    "A": _v2_entry(
        "PU0,0;PD1000,1000;",
        (0, 0, 1000, 1000),
        [(0, 0), (1000, 1000)],
        [(0, 0), (1000, 1000)],
    ),
    "B": _v2_entry(
        "PU0,0;PD0,1000;",
        (0, 0, 0, 1000),
        [(0, 0), (0, 1000)],
        [(0, 0), (0, 1000)],
    ),
    "C": _v2_entry(
        "PU0,500;PD0,500;PD;AA500,500,360;",
        (0, 0, 1000, 1000),
        [(500, 0), (0, 500), (500, 1000)],
        [(500, 0), (1000, 500), (500, 1000)],
    ),
    "x": _v2_entry(
        "PU0,0;PD500,500;",
        (0, 0, 500, 500),
        [(0, 0), (500, 500)],
        [(0, 0), (500, 500)],
    ),
    "g": _v2_entry(
        "PU0,-500;PD500,500;",
        (0, -500, 500, 500),
        [(0, -500), (500, 500)],
        [(0, -500), (500, 500)],
    ),
}


@pytest.fixture
def synth_lib(tmp_path: Path) -> Path:
    """A synthetic v2 font library path (font name ``Test``)."""
    return _write_library(tmp_path, _SYNTH_CHARACTERS)


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

    def test_reference_char_height_is_exact(self) -> None:
        """The reference character 'E' renders at exactly the target height."""
        block = render_text_line_plt_font("E", 0.5, "dino")
        bounds = block.bounds()
        assert bounds is not None
        min_x, min_y, max_x, max_y = bounds
        assert math.isclose(max_y - min_y, 0.5, rel_tol=1e-9)
        assert math.isclose(min_y, 0.0, abs_tol=1e-9)  # baseline anchored
        assert math.isclose(min_x, 0.0, abs_tol=1e-9)
        assert max_x > min_x

    def test_renders_upright_nonempty(self) -> None:
        """A real multi-glyph string renders upright with sane bounds."""
        block = render_text_line_plt_font("DINO ARC", 0.5, "dino", cutter_diameter=0.04)
        bounds = block.bounds()
        assert bounds is not None
        min_x, _min_y, max_x, max_y = bounds
        assert math.isclose(min_x, 0.0, abs_tol=1e-9)
        # Cap-height glyphs reach the target; round glyphs may overshoot
        # their bbox slightly (overshoot/undershoot of the engraved sheet).
        assert 0.5 <= max_y <= 0.51
        assert max_x > min_x

    def test_descenders_hang_below_baseline(self) -> None:
        """Descender glyphs (``p``) extend below the baseline anchor."""
        block = render_text_line_plt_font("p", 0.5, "Dino")
        bounds = block.bounds()
        assert bounds is not None
        assert bounds[1] < 0.0

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
    """Baseline anchoring, kerning and space math against synthetic glyphs."""

    def test_envelope_kerning_formula(self, synth_lib: Path) -> None:
        """Adjacent glyph strokes sit exactly ``cutter + char_spacing`` apart.

        A's right silhouette is the diagonal ``x = y``; B's left silhouette
        is the spine at ``x = 0``. The maximum penetration is A's full width
        (1.0 at the shared top), so B's origin lands one glyph-width plus
        the clearance after A's origin.
        """
        height = 1.0
        cutter = 0.04
        spacing = 0.02
        block = _render("AB", height, synth_lib, cutter_diameter=cutter, character_spacing=spacing)
        bounds = block.bounds()
        assert bounds is not None
        # A spans [0, 1.0]; B (zero-width) sits clearance past A's right tip.
        expected_width = 1.0 + cutter + spacing
        assert bounds[2] == pytest.approx(expected_width, abs=1e-9)
        # Baseline anchor: nothing below y=0 for these glyphs.
        assert bounds[1] == pytest.approx(0.0, abs=1e-12)

    def test_kerning_uses_clearance_not_height_fraction(self, synth_lib: Path) -> None:
        """Doubling the text height does not widen the kerning clearance."""
        low = _render("AB", 1.0, synth_lib, cutter_diameter=0.04, character_spacing=0.02)
        high = _render("AB", 2.0, synth_lib, cutter_diameter=0.04, character_spacing=0.02)
        low_bounds = low.bounds()
        high_bounds = high.bounds()
        assert low_bounds is not None and high_bounds is not None
        # Kerning offset scales with the glyph (1.0 -> 2.0) but the clearance
        # stays a fixed inch value: 2 * 1.0 + 0.06, not 2 * (1.0 + 0.06).
        assert high_bounds[2] == pytest.approx(2.0 + 0.04 + 0.02, abs=1e-9)

    def test_space_advance_formula(self, synth_lib: Path) -> None:
        """A space contributes its own 0.3h advance (no kerning across it)."""
        height = 1.0
        block = _render("A A", height, synth_lib, cutter_diameter=0.0, character_spacing=0.0)
        bounds = block.bounds()
        assert bounds is not None
        # A(1.0) + space(0.3) + A(1.0): kerning applies only between adjacent
        # glyphs WITHIN a word, never across a space.
        expected = 1.0 + SPACE_HEIGHT_FRACTION + 1.0
        assert bounds[2] == pytest.approx(expected, abs=1e-9)

    def test_space_advance_configurable(self, synth_lib: Path) -> None:
        """``space_width_fraction`` overrides the 0.3 default."""
        block = _render(
            "A A",
            1.0,
            synth_lib,
            cutter_diameter=0.0,
            character_spacing=0.0,
            space_width_fraction=0.5,
        )
        bounds = block.bounds()
        assert bounds is not None
        assert bounds[2] == pytest.approx(1.0 + 0.5 + 1.0, abs=1e-9)

    def test_space_advance_adds_character_spacing(self, synth_lib: Path) -> None:
        """A space advances ``fraction * height + character_spacing``."""
        block = _render("A A", 1.0, synth_lib, cutter_diameter=0.04, character_spacing=0.02)
        bounds = block.bounds()
        assert bounds is not None
        assert bounds[2] == pytest.approx(1.0 + 0.3 + 0.02 + 1.0, abs=1e-9)

    def test_min_glyph_width_floors_zero_width_kerning(self, synth_lib: Path) -> None:
        """``min_glyph_width`` clamps the left silhouette outward."""
        # Zero-width B pair: pure envelope kerning stacks them at the
        # clearance; a 0.25 floor forces a full 0.25 advance.
        tight = _render("BB", 1.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0)
        floored = _render(
            "BB", 1.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0, min_glyph_width=0.25
        )
        assert tight.bounds() is not None and floored.bounds() is not None
        assert tight.bounds()[2] == pytest.approx(0.0, abs=1e-9)
        assert floored.bounds()[2] == pytest.approx(0.25, abs=1e-9)

    def test_min_glyph_width_is_absolute_inches(self, synth_lib: Path) -> None:
        """The floor is an absolute inch value, independent of text height."""
        floored = _render(
            "BB", 2.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0, min_glyph_width=0.25
        )
        assert floored.bounds() is not None
        assert floored.bounds()[2] == pytest.approx(0.25, abs=1e-9)

    def test_kerning_window_fraction_threads_through(self, synth_lib: Path) -> None:
        """The fraction reaches the kerning math and can widen the advance.

        C's right silhouette is ``0.5/1.0/0.5`` at ``y=0/0.5/1.0``; A's
        left silhouette is the diagonal ``x = y``. The same-height
        penetration profile is ``[0.5, 0.5, -0.5]`` (max 0.5, so A's origin
        lands at 0.5 and the line spans 1.5). A window of 0.5 (fraction
        1.0) reaches past the sample spacing: stage 1 pairs the mid poke
        (x=1.0) with the y=0 silhouette (x=0.0), deepening it to 1.0 and
        giving ``[0.5, 1.0, 0.0]`` -- the max of the windowed profile
        keeps it whole, so the line widens to 2.0.
        """
        historical = _render(
            "CA",
            1.0,
            synth_lib,
            cutter_diameter=0.0,
            character_spacing=0.0,
            kerning_window_fraction=0.0,
        )
        default = _render("CA", 1.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0)
        assert historical.bounds() is not None and default.bounds() is not None
        # The default 0.05 window (half-width 0.025) stays below the 0.5
        # sample spacing of this coarse synthetic font, so it is a no-op.
        assert historical.bounds()[2] == pytest.approx(1.5, abs=1e-9)
        assert default.bounds()[2] == pytest.approx(1.5, abs=1e-9)
        wide = _render(
            "CA",
            1.0,
            synth_lib,
            cutter_diameter=0.0,
            character_spacing=0.0,
            kerning_window_fraction=1.0,
        )
        assert wide.bounds() is not None
        assert wide.bounds()[2] == pytest.approx(2.0, abs=1e-9)

    def test_cap_height_scaling(self, synth_lib: Path) -> None:
        """The reference glyph scales to exactly the target height."""
        block = _render("A", 0.8, synth_lib)
        bounds = block.bounds()
        assert bounds is not None
        assert bounds[3] - bounds[1] == pytest.approx(0.8, rel=1e-9)

    def test_relative_glyph_sizes_preserved(self, synth_lib: Path) -> None:
        """Short glyphs stay proportionally short (uniform scale)."""
        block = _render("Ax", 0.8, synth_lib)
        # 'x' is half the design height of 'A': its strokes span ~0.4in.
        x_strokes = block.strokes[1:]
        x_ys = [
            z.imag for stroke in x_strokes for seg in stroke.segments for z in (seg.start, seg.end)
        ]
        assert max(x_ys) - min(x_ys) == pytest.approx(0.4, rel=1e-6)

    def test_baseline_anchor_and_left_edge(self, synth_lib: Path) -> None:
        """The first glyph's left edge sits at x=0 on the baseline y=0."""
        block = _render("CB", 0.5, synth_lib)
        bounds = block.bounds()
        assert bounds is not None
        assert bounds[0] == pytest.approx(0.0, abs=1e-12)
        assert bounds[1] == pytest.approx(0.0, abs=1e-12)

    def test_descenders_below_baseline(self, synth_lib: Path) -> None:
        """A descender glyph reaches below the baseline anchor."""
        block = _render("g", 1.0, synth_lib)
        bounds = block.bounds()
        assert bounds is not None
        assert bounds[1] == pytest.approx(-0.5, abs=1e-9)
        assert bounds[3] == pytest.approx(0.5, abs=1e-9)

    def test_arcs_measure_swept_extent(self, synth_lib: Path) -> None:
        """A glyph of native arcs contributes its swept box, not a chord box."""
        block = _render("C", 1.0, synth_lib)
        assert _count_arcs(block) == 1
        bounds = block.bounds()
        assert bounds is not None
        # Full-circle glyph: swept bounds == the circle's box (1.0 x 1.0).
        assert bounds[2] - bounds[0] == pytest.approx(1.0, abs=1e-9)
        assert bounds[3] - bounds[1] == pytest.approx(1.0, abs=1e-9)

    def test_sweeps_verbatim(self, synth_lib: Path) -> None:
        """v2 storage is +y up: arc sweeps pass through without negation."""
        # Stored C sweeps +360; the render frame keeps it verbatim.
        block = _render("C", 1.0, synth_lib)
        arc = next(
            seg for stroke in block.strokes for seg in stroke.segments if isinstance(seg, ArcSeg)
        )
        assert arc.sweep_deg == pytest.approx(360.0)


class TestEnvelopeHelpers:
    """Pure envelope interpolation and kerning math."""

    def test_interpolate_linear_between_samples(self) -> None:
        """Midpoints interpolate linearly between neighbouring samples."""
        envelope = [(0.0, 0.0), (10.0, 100.0)]
        assert interpolate_envelope(envelope, 50.0) == pytest.approx(5.0)

    def test_interpolate_clamps_outside_range(self) -> None:
        """Heights outside the sampled range clamp to the end samples."""
        envelope = [(1.0, 10.0), (2.0, 20.0)]
        assert interpolate_envelope(envelope, 0.0) == 1.0
        assert interpolate_envelope(envelope, 99.0) == 2.0

    def test_interpolate_empty_envelope(self) -> None:
        """An empty envelope has no silhouette to evaluate."""
        assert interpolate_envelope([], 5.0) is None

    def test_kerning_offset_uses_max_penetration(self) -> None:
        """The offset is the worst-case right-into-left penetration."""
        left = _GlyphGeometry(
            block=TextBlock.empty(),
            bounding_box=(0.0, 0.0, 1.0, 1.0),
            left_envelope=(),
            right_envelope=((0.0, 0.0), (1.0, 1.0)),
        )
        right = _GlyphGeometry(
            block=TextBlock.empty(),
            bounding_box=(0.0, 0.0, 0.5, 1.0),
            left_envelope=((0.5, 0.0), (0.0, 1.0)),
            right_envelope=(),
        )
        # At y=1.0: right_x=1.0, left_x=0.0 -> penetration 1.0.
        assert kerning_offset(left, right) == pytest.approx(1.0)

    def test_kerning_offset_min_width_clamps(self) -> None:
        """The minimum width clamps a zero right silhouette outward."""
        zero = _GlyphGeometry(
            block=TextBlock.empty(),
            bounding_box=(0.0, 0.0, 0.0, 1.0),
            left_envelope=((0.0, 0.0), (0.0, 1.0)),
            right_envelope=((0.0, 0.0), (0.0, 1.0)),
        )
        assert kerning_offset(zero, zero, min_glyph_width_design=0.0) == pytest.approx(0.0)
        assert kerning_offset(zero, zero, min_glyph_width_design=0.25) == pytest.approx(0.25)

    def test_kerning_offset_no_overlap_falls_back_to_width(self) -> None:
        """Glyphs without overlapping height advance by the left bbox width."""
        low = _GlyphGeometry(
            block=TextBlock.empty(),
            bounding_box=(0.0, -1.0, 0.4, 0.0),
            left_envelope=(),
            right_envelope=(),
        )
        high = _GlyphGeometry(
            block=TextBlock.empty(),
            bounding_box=(0.0, 0.5, 1.0, 1.5),
            left_envelope=(),
            right_envelope=(),
        )
        assert kerning_offset(low, high) == pytest.approx(0.4)

    @staticmethod
    def _geometry(
        bounding_box: Tuple[float, float, float, float],
        left: Tuple[Tuple[float, float], ...] = (),
        right: Tuple[Tuple[float, float], ...] = (),
    ) -> _GlyphGeometry:
        """Build a bare geometry carrier for pure kerning math tests."""
        return _GlyphGeometry(
            block=TextBlock.empty(),
            bounding_box=bounding_box,
            left_envelope=left,
            right_envelope=right,
        )

    def test_kerning_window_zero_matches_same_height_max(self) -> None:
        """A zero window reproduces the historical same-height comparison."""
        left = self._geometry((0.0, 0.0, 1.0, 1.0), right=((0.0, 0.0), (1.0, 0.5), (0.0, 1.0)))
        right = self._geometry((0.0, 0.0, 0.4, 1.0), left=((0.4, 0.0), (0.4, 1.0)))
        legacy = kerning_offset(left, right)
        assert kerning_offset(left, right, window_design=0.0) == pytest.approx(legacy)
        # The poke at y=0.5 (x=1.0) vs the flat left silhouette (x=0.4) is
        # the same-height worst case: penetration 0.6.
        assert legacy == pytest.approx(0.6)

    def test_kerning_window_detects_staggered_pokes(self) -> None:
        """Pokes at different heights count once the window spans the offset.

        The left glyph pokes right at y=0.5 (x=1.0); the right glyph pokes
        left at y=0.7 (x=0.0). Same-height sampling only sees penetrations
        of 0.6; a 0.2 window pairs the pokes and raises the offset.
        """
        left = self._geometry((0.0, 0.0, 1.0, 1.0), right=((0.0, 0.0), (1.0, 0.5), (0.0, 1.0)))
        right = self._geometry(
            (0.0, 0.0, 0.4, 1.0),
            left=((0.4, 0.0), (0.4, 0.6), (0.0, 0.7), (0.4, 1.0)),
        )
        assert kerning_offset(left, right, window_design=0.0) == pytest.approx(0.6)
        # Stage 1 pairs y=0.5 (x=1.0) with the y=0.7 poke (x=0.0) -> p=1.0,
        # and the max of the windowed profile keeps it whole.
        assert kerning_offset(left, right, window_design=0.2) == pytest.approx(1.0)

    def test_kerning_window_never_narrows_the_advance(self) -> None:
        """The window only ever widens the advance, never loosens it.

        Against a flat opposing silhouette there is no staggered poke to
        find, so the worst windowed penetration stays the same-height
        worst case -- a localized poke (``AP``-like) and a sustained one
        (``db``-like) both keep their full tightening.
        """
        flat_left = ((0.0, 0.0), (0.0, 1.0))
        localized = self._geometry((0.0, 0.0, 1.0, 1.0), right=((0.0, 0.0), (1.0, 0.5), (0.0, 1.0)))
        sustained = self._geometry((0.0, 0.0, 1.0, 1.0), right=((1.0, 0.0), (1.0, 1.0)))
        receiving = self._geometry((0.0, 0.0, 0.4, 1.0), left=flat_left)
        assert kerning_offset(localized, receiving) == pytest.approx(1.0)
        assert kerning_offset(sustained, receiving) == pytest.approx(1.0)
        # Widening the window cannot dilute either pair: the maximum of the
        # windowed penetrations is bounded below by the same-height maximum.
        assert kerning_offset(localized, receiving, window_design=0.5) == pytest.approx(1.0)
        assert kerning_offset(sustained, receiving, window_design=0.5) == pytest.approx(1.0)


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
        lib = _write_library(
            tmp_path,
            {
                "A": _v2_entry("   ", (0, 0, 0, 0)),
                "B": _SYNTH_CHARACTERS["B"],
            },
        )
        block = _render("AB", 1.0, lib)
        # Only B contributes; the line still scales from the design height.
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
        lib_a = _write_library(
            tmp_path / "a", {"A": _v2_entry("PU0,0;PD1000,1000;", (0, 0, 1000, 1000))}
        )
        lib_b = _write_library(
            tmp_path / "b", {"A": _v2_entry("PU0,0;PD3000,1000;", (0, 0, 3000, 1000))}
        )
        narrow = _render("A", 1.0, lib_a)
        wide = _render("A", 1.0, lib_b)
        narrow_bounds = narrow.bounds()
        wide_bounds = wide.bounds()
        assert narrow_bounds is not None and wide_bounds is not None
        assert wide_bounds[2] > narrow_bounds[2]

    def test_clear_glyph_cache_picks_up_new_library(self, tmp_path: Path) -> None:
        """Editing a library in place is visible after clear_glyph_cache()."""
        lib = tmp_path / "plt_fonts.json"
        _write_library(tmp_path, {"A": _v2_entry("PU0,0;PD1000,1000;", (0, 0, 1000, 1000))})
        first = _render("A", 1.0, lib)
        _write_library(tmp_path, {"A": _v2_entry("PU0,0;PD3000,1000;", (0, 0, 3000, 1000))})
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

    def test_module_exposes_new_defaults(self) -> None:
        """The v2 layout defaults are the module's documented contract."""
        assert SPACE_HEIGHT_FRACTION == pytest.approx(0.3)
        assert MIN_GLYPH_WIDTH == pytest.approx(0.0)
        assert KERNING_WINDOW_FRACTION == pytest.approx(0.05)


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
        # ResolvedTextLine defaults for the v2 typesetting fields.
        assert calls["space_width_fraction"] == pytest.approx(0.3)
        assert calls["min_glyph_width"] == pytest.approx(0.0)
        assert calls["kerning_window_fraction"] == pytest.approx(0.05)
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
        """Arc counts per label are pinned (glyph parsing is deterministic).

        Re-pinned for the v2 envelope-kerned typesetter: tighter kerning
        keeps lines narrower, so less width-compression flattens arcs and
        more native ``AA`` commands survive end-to-end.
        """
        rendered = self._rendered()
        counts = {key: content.plt_content.count("AA") for key, (_rl, content) in rendered.items()}
        assert counts["dino_banner"] == 177
        assert counts["mixed_tag"] == 373
        assert counts["ttf_card"] == 0
