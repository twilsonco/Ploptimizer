"""Tests for the arc-native PLT-extracted font renderer (v2 schema).

The bundled ``fonts/VP-EL-fonts/catalog/`` provides the real glyph library; a
synthetic v2 library (``tmp_path``) pins the exact layout math (baseline
anchoring, envelope kerning, space advance) with hand-computable glyph
geometry.
"""

from __future__ import annotations

import json
import math
import sys
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
    MIN_GLYPH_WIDTH_FRACTION,
    SPACE_HEIGHT_FRACTION,
    PltFontRenderError,
    _capline_geometry,
    _GlyphGeometry,
    clear_glyph_cache,
    interpolate_envelope,
    kerning_offset,
    render_text_line_plt_font,
    render_text_line_plt_font_with_glyphs,
    render_text_line_plt_font_with_words,
)
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine
from plt_optimizer.generate.text_geometry import ArcSeg, LineSeg, Stroke, TextBlock


def _load_glyph_transforms() -> Any:
    """Import ``fonts/glyph_transforms.py`` by path for its tuning constants.

    The derived-glyph ratios asserted below follow the shared recipe constants,
    so they stay valid when the shop retunes them.

    Returns:
        The loaded module.
    """
    import importlib.util

    module_path = Path(__file__).resolve().parents[1] / "fonts" / "glyph_transforms.py"
    spec = importlib.util.spec_from_file_location("glyph_transforms", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("glyph_transforms", module)
    spec.loader.exec_module(module)
    return module


_gt = _load_glyph_transforms()


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
    """Write a synthetic v2 font catalog holding one ``Test`` font.

    Args:
        root: Directory to write the ``catalog/`` subdirectory into.
        characters: Character -> v2 entry mapping for the ``Test`` font.

    Returns:
        The catalog directory (usable as the renderer's ``library_dir``).
    """
    catalog = root / "catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    (catalog / "Test.json").write_text(
        json.dumps(
            {
                "font_name": "Test",
                "reference_char": "A",
                "declared_height_in": 1.0,
                "reference_char_height_in": 1.0,
                "normalized_ref_height": 1.0,
                "characters": characters,
            }
        ),
        encoding="utf-8",
    )
    return catalog


# Hand-computable v2 glyph set (plotter units, 1000/in, baseline y=0, +y up,
# left edge x=0):
#   A: diagonal (0,0)->(1000,1000) (1.0 x 1.0 in), polyline-only. Its left
#      and right silhouettes are both x = y (a single stroke).
#   B: zero-width vertical line on the baseline (0.0 x 1.0 in).
#   C: full circle radius 0.5 centered (500,500) (1.0 x 1.0 in), one native
#      AA arc (the parser opens arcs from a PD, matching extracted fonts).
#   x: short diagonal (0,0)->(500,500) for relative-size checks.
#   g: descender diagonal (0,-500)->(500,500) (hangs below the baseline).
#   T: thin vertical bar 0.1 wide, full height (right silhouette x=0.1) for
#      the advance-floor cap (a thin glyph's floor is capped by its width).
#   R: far-right vertical bar at x=0.95..1.0 (left silhouette x=0.95) so an
#      "A" before it barely penetrates (max p=0.05), below the floor.
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
    "T": _v2_entry(
        "PU0,0;PD0,1000;PU100,0;PD100,1000;",
        (0, 0, 100, 1000),
        [(0, 0), (0, 1000)],
        [(100, 0), (100, 1000)],
    ),
    "R": _v2_entry(
        "PU950,0;PD950,1000;PU1000,0;PD1000,1000;",
        (950, 0, 1000, 1000),
        [(950, 0), (950, 1000)],
        [(1000, 0), (1000, 1000)],
    ),
}


@pytest.fixture
def synth_lib(tmp_path: Path) -> Path:
    """A synthetic v2 font library path (font name ``Test``)."""
    return _write_library(tmp_path, _SYNTH_CHARACTERS)


def _render(text: str, height: float, lib: Path, font: str = "Test", **kwargs: object) -> TextBlock:
    """Render ``text`` from the synthetic ``Test`` font."""
    return render_text_line_plt_font(text, height, font, library_dir=lib, **kwargs)  # type: ignore[arg-type]


def _count_arcs(block: TextBlock) -> int:
    """Count arc segments in a rendered block."""
    return sum(1 for stroke in block.strokes for seg in stroke.segments if isinstance(seg, ArcSeg))


def _count_lines(block: TextBlock) -> int:
    """Count line segments in a rendered block."""
    return sum(1 for stroke in block.strokes for seg in stroke.segments if isinstance(seg, LineSeg))


class TestBundledLibraryRendering:
    """The shipped font catalog renders through the arc-native path."""

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

    def test_min_glyph_width_floors_advance_capped_by_glyph_width(self, synth_lib: Path) -> None:
        """``min_glyph_width_fraction`` floors the advance, capped by the left width.

        ``T`` is a thin 0.1-wide bar; a 0.25 floor is capped by T's own
        width so the pair advances by 0.1 (not 0.25), and a 0.0 floor lets
        the (negative) penetration floor at 0.0. This is the fix for the
        historical silhouette clamp, which inflated every thin-left-glyph
        pair to the full floor and made it scale-insensitive.
        """
        tight = _render("TR", 1.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0)
        floored = _render(
            "TR",
            1.0,
            synth_lib,
            cutter_diameter=0.0,
            character_spacing=0.0,
            min_glyph_width_fraction=0.25,
        )
        assert tight.bounds() is not None and floored.bounds() is not None
        # R spans [0.95, 1.0] from its origin; T sits at 0.
        # 0.0 floor: advance max(-0.85, 0.0) = 0.0 -> R right edge at 1.0.
        assert tight.bounds()[2] == pytest.approx(1.0, abs=1e-9)
        # 0.25 floor capped by T's 0.1 width -> advance 0.1 -> R right at 1.1.
        assert floored.bounds()[2] == pytest.approx(1.1, abs=1e-9)

    def test_zero_width_glyph_floors_at_zero(self, synth_lib: Path) -> None:
        """A zero-width left glyph floors at 0.0 (relies on the clearance).

        ``B`` is zero-width, so ``min(left_width, floor)`` is 0.0 for any
        floor; its stroke-to-stroke air comes from the clearance term, not
        the floor.
        """
        floored = _render(
            "BB",
            1.0,
            synth_lib,
            cutter_diameter=0.02,
            character_spacing=0.0,
            min_glyph_width_fraction=0.25,
        )
        assert floored.bounds() is not None
        # Advance 0.0 + clearance 0.02; the second B is zero-width.
        assert floored.bounds()[2] == pytest.approx(0.02, abs=1e-9)

    def test_min_glyph_width_scales_with_height(self, synth_lib: Path) -> None:
        """The floor is a cap-height fraction: it scales with the height.

        ``A`` (1.0 wide at height 1.0, 2.0 wide at height 2.0) is wider than
        the 0.25 floor at both heights, so the floor binds at
        ``0.25 * height`` -- 0.25in at height 1.0 and 0.5in at height 2.0.
        This is the unitless-fraction contract: the engraved air is
        proportional to the text size.
        """
        low = _render(
            "AR",
            1.0,
            synth_lib,
            cutter_diameter=0.0,
            character_spacing=0.0,
            min_glyph_width_fraction=0.25,
        )
        high = _render(
            "AR",
            2.0,
            synth_lib,
            cutter_diameter=0.0,
            character_spacing=0.0,
            min_glyph_width_fraction=0.25,
        )
        assert low.bounds() is not None and high.bounds() is not None
        # The floor advance is 0.25 * height; R's own extent scales with the
        # height too, so the right edge is advance + extent.
        assert low.bounds()[2] == pytest.approx(0.25 + 1.0, abs=1e-9)
        assert high.bounds()[2] == pytest.approx(0.5 + 2.0, abs=1e-9)

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

    def test_kerning_min_gap_threads_through(self, synth_lib: Path) -> None:
        """``kerning_min_gap_fraction`` adds uniform air to every kerned pair."""
        # Baseline CA: penetration 0.5, zero clearance -> total width 1.5.
        baseline = _render("CA", 1.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0)
        gapped = _render(
            "CA",
            1.0,
            synth_lib,
            cutter_diameter=0.0,
            character_spacing=0.0,
            kerning_min_gap_fraction=0.25,
        )
        assert baseline.bounds() is not None and gapped.bounds() is not None
        assert baseline.bounds()[2] == pytest.approx(1.5, abs=1e-9)
        assert gapped.bounds()[2] == pytest.approx(1.75, abs=1e-9)

    def test_kerning_penetration_scale_threads_through(self, synth_lib: Path) -> None:
        """``kerning_penetration_scale`` over-kerns the detected penetration."""
        # Baseline CA penetration 0.5; at 2.0 the advance doubles to 1.0.
        scaled = _render(
            "CA",
            1.0,
            synth_lib,
            cutter_diameter=0.0,
            character_spacing=0.0,
            kerning_penetration_scale=2.0,
        )
        assert scaled.bounds() is not None
        assert scaled.bounds()[2] == pytest.approx(2.0, abs=1e-9)

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

    def test_kerning_offset_min_width_floors_capped_by_left_width(self) -> None:
        """The floor is capped by the left glyph's own bbox width."""
        # Zero-width glyph: min(0.0, floor) == 0.0, so the floor never
        # inflates a zero-width pair (its air comes from the clearance).
        zero = _GlyphGeometry(
            block=TextBlock.empty(),
            bounding_box=(0.0, 0.0, 0.0, 1.0),
            left_envelope=((0.0, 0.0), (0.0, 1.0)),
            right_envelope=((0.0, 0.0), (0.0, 1.0)),
        )
        assert kerning_offset(zero, zero, min_glyph_width_design=0.0) == pytest.approx(0.0)
        assert kerning_offset(zero, zero, min_glyph_width_design=0.25) == pytest.approx(0.0)

        # Thin (0.1-wide) left glyph with a fully recessed right glyph: the
        # advance floors at the left glyph's own width, not the full floor.
        thin = _GlyphGeometry(
            block=TextBlock.empty(),
            bounding_box=(0.0, 0.0, 0.1, 1.0),
            left_envelope=((0.0, 0.0), (0.0, 1.0)),
            right_envelope=((0.1, 0.0), (0.1, 1.0)),
        )
        far = _GlyphGeometry(
            block=TextBlock.empty(),
            bounding_box=(0.95, 0.0, 1.0, 1.0),
            left_envelope=((0.95, 0.0), (0.95, 1.0)),
            right_envelope=((1.0, 0.0), (1.0, 1.0)),
        )
        assert kerning_offset(thin, far, min_glyph_width_design=0.25) == pytest.approx(0.1)

        # Wide (1.0) left glyph: the full 0.25 floor binds (below its width).
        wide = _GlyphGeometry(
            block=TextBlock.empty(),
            bounding_box=(0.0, 0.0, 1.0, 1.0),
            left_envelope=((0.0, 0.0), (0.0, 1.0)),
            right_envelope=((0.0, 0.0), (0.0, 1.0)),
        )
        assert kerning_offset(wide, far, min_glyph_width_design=0.25) == pytest.approx(0.25)

    def test_kerning_offset_recession_scale(self) -> None:
        """A recessed pair scales by ``recession_scale``, not the penetration dial.

        The thin/far pair has penetration -0.85 (fully recessed). The
        penetration scale must not touch it; the recession scale does, and
        the non-negative floor keeps the advance >= 0.
        """
        thin = _GlyphGeometry(
            block=TextBlock.empty(),
            bounding_box=(0.0, 0.0, 0.1, 1.0),
            left_envelope=((0.0, 0.0), (0.0, 1.0)),
            right_envelope=((0.1, 0.0), (0.1, 1.0)),
        )
        far = _GlyphGeometry(
            block=TextBlock.empty(),
            bounding_box=(0.95, 0.0, 1.0, 1.0),
            left_envelope=((0.95, 0.0), (0.95, 1.0)),
            right_envelope=((1.0, 0.0), (1.0, 1.0)),
        )
        # Default recession 1.0: -0.85, floored up to the 0.0 floor -> 0.0.
        assert kerning_offset(thin, far, penetration_scale=0.5) == pytest.approx(0.0)
        # Recession 2.0 deepens the recess (-1.7) but the floor keeps 0.0.
        assert kerning_offset(
            thin, far, penetration_scale=0.5, recession_scale=2.0
        ) == pytest.approx(0.0)
        # A positive-penetration pair ignores the recession scale entirely.
        diagonal = _GlyphGeometry(
            block=TextBlock.empty(),
            bounding_box=(0.0, 0.0, 1.0, 1.0),
            left_envelope=((0.0, 0.0), (1.0, 1.0)),
            right_envelope=((0.0, 0.0), (1.0, 1.0)),
        )
        spine = _GlyphGeometry(
            block=TextBlock.empty(),
            bounding_box=(0.0, 0.0, 0.0, 1.0),
            left_envelope=((0.0, 0.0), (0.0, 1.0)),
            right_envelope=((0.0, 0.0), (0.0, 1.0)),
        )
        # Penetration 1.0 at the shared top; recession scale is irrelevant.
        assert kerning_offset(
            diagonal, spine, penetration_scale=0.5, recession_scale=3.0
        ) == pytest.approx(0.5)

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

    def test_penetration_scale_scales_the_detected_maximum(self) -> None:
        """``penetration_scale`` multiplies the detected penetration."""
        left = self._geometry((0.0, 0.0, 1.0, 1.0), right=((0.0, 0.0), (1.0, 0.5), (0.0, 1.0)))
        right = self._geometry((0.0, 0.0, 0.4, 1.0), left=((0.4, 0.0), (0.4, 1.0)))
        # Same-height worst case: the y=0.5 poke (x=1.0) vs x=0.4 -> 0.6.
        assert kerning_offset(left, right) == pytest.approx(0.6)
        assert kerning_offset(left, right, penetration_scale=1.5) == pytest.approx(0.9)
        assert kerning_offset(left, right, penetration_scale=0.0) == pytest.approx(0.0)

    def test_extended_sampling_detects_edge_pokes(self) -> None:
        """A poke just outside the y-overlap counts once the window spans it.

        The left glyph (bbox height 1.0) pokes right at y=0.9 (x=1.0); the
        right glyph only spans y in [0, 0.8] with a flat left silhouette at
        x=0.4. Same-height sampling stops at the overlap top (y=0.8) and
        misses the poke (max ~0.489); a 0.2 window extends sampling to
        y=1.0, where the poke compares against the flat silhouette's
        nearest material (x=0.4) for a full 0.6 penetration.
        """
        left = self._geometry((0.0, 0.0, 1.0, 1.0), right=((0.0, 0.0), (1.0, 0.9), (0.0, 1.0)))
        right = self._geometry((0.0, 0.0, 0.4, 0.8), left=((0.4, 0.0), (0.4, 0.8)))
        # Same-height: worst sample inside [0, 0.8] sits on the poke ramp at
        # y=0.8 (x=8/9 vs the flat x=0.4 -> 22/45); the y=0.9 poke (x=1.0)
        # is outside the overlap entirely.
        assert kerning_offset(left, right, window_design=0.0) == pytest.approx(22.0 / 45.0)
        # Window 0.2 reaches y=1.0: the y=0.9 poke (x=1.0) compares against
        # the flat silhouette clamped to its nearest material (x=0.4).
        assert kerning_offset(left, right, window_design=0.2) == pytest.approx(0.6)

    def test_fallback_fraction_scales_no_overlap_advance(self) -> None:
        """``fallback_fraction`` scales the bbox-width fallback advance."""
        low = self._geometry((0.0, -1.0, 0.4, 0.0))
        high = self._geometry((0.0, 0.5, 1.0, 1.5))
        assert kerning_offset(low, high) == pytest.approx(0.4)
        assert kerning_offset(low, high, fallback_fraction=1.5) == pytest.approx(0.6)
        assert kerning_offset(low, high, fallback_fraction=0.0) == pytest.approx(0.0)


class TestWordGroups:
    """with_words partitions strokes exactly by construction."""

    def test_word_groups_partition_strokes(self, synth_lib: Path) -> None:
        """Word groups are contiguous stroke-index slices covering all strokes."""
        block, groups = render_text_line_plt_font_with_words(
            "A B A", 0.5, "Test", library_dir=synth_lib
        )
        assert [word for word, _ in groups] == ["A", "B", "A"]
        all_indices = [i for _word, indices in groups for i in indices]
        assert sorted(all_indices) == list(range(len(block.strokes)))
        for _word, indices in groups:
            assert indices == sorted(indices)

    def test_blank_segment_yields_empty_group(self, synth_lib: Path) -> None:
        """A double space keeps column alignment with an empty group."""
        _block, groups = render_text_line_plt_font_with_words(
            "A  B", 0.5, "Test", library_dir=synth_lib
        )
        assert [word for word, _ in groups] == ["A", "", "B"]
        assert groups[1][1] == []

    def test_empty_text(self, synth_lib: Path) -> None:
        """Empty input yields an empty block and no groups."""
        block, groups = render_text_line_plt_font_with_words("", 0.5, "Test", library_dir=synth_lib)
        assert block.is_empty()
        assert groups == []

    def test_blank_only_text_keeps_word_structure(self, synth_lib: Path) -> None:
        """A spaces-only line renders nothing but reports its word slots."""
        block, groups = render_text_line_plt_font_with_words(
            "  ", 0.5, "Test", library_dir=synth_lib
        )
        assert block.is_empty()
        assert groups == [("", []), ("", []), ("", [])]


class TestGlyphGroups:
    """with_glyphs partitions strokes per character, exact by construction."""

    def test_glyph_groups_partition_strokes(self, synth_lib: Path) -> None:
        """Every stroke belongs to exactly one glyph group, in text order."""
        block, _words, glyphs = render_text_line_plt_font_with_glyphs(
            "ABA", 0.5, "Test", library_dir=synth_lib
        )
        assert [char for char, _indices in glyphs] == ["A", "B", "A"]
        all_indices = [i for _char, indices in glyphs for i in indices]
        assert sorted(all_indices) == list(range(len(block.strokes)))
        for _char, indices in glyphs:
            assert indices == sorted(indices)

    def test_multi_stroke_glyph_owns_a_contiguous_slice(self, synth_lib: Path) -> None:
        """A two-stroke glyph (``T``) gets one group of two stroke indices."""
        _block, _words, glyphs = render_text_line_plt_font_with_glyphs(
            "TA", 0.5, "Test", library_dir=synth_lib
        )
        assert glyphs[0][0] == "T"
        assert len(glyphs[0][1]) == 2
        assert glyphs[0][1] == [0, 1]
        assert glyphs[1] == ("A", [2])

    def test_space_contributes_no_group(self, synth_lib: Path) -> None:
        """Spaces advance the cursor but own no strokes and no group."""
        block, _words, glyphs = render_text_line_plt_font_with_glyphs(
            "A B", 0.5, "Test", library_dir=synth_lib
        )
        assert [char for char, _indices in glyphs] == ["A", "B"]
        all_indices = [i for _char, indices in glyphs for i in indices]
        assert sorted(all_indices) == list(range(len(block.strokes)))

    def test_empty_text(self, synth_lib: Path) -> None:
        """Empty input yields an empty block and no glyph groups."""
        block, _words, glyphs = render_text_line_plt_font_with_glyphs(
            "", 0.5, "Test", library_dir=synth_lib
        )
        assert block.is_empty()
        assert glyphs == []

    def test_with_words_wrapper_matches_the_glyph_renderer(self, synth_lib: Path) -> None:
        """The word wrapper returns the identical block and word groups."""
        block_w, words_w = render_text_line_plt_font_with_words(
            "A B A", 0.5, "Test", library_dir=synth_lib
        )
        block_g, words_g, _glyphs = render_text_line_plt_font_with_glyphs(
            "A B A", 0.5, "Test", library_dir=synth_lib
        )
        assert block_g.to_hpgl() == block_w.to_hpgl()
        assert words_g == words_w


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
                render_text_line_plt_font("A", 0.5, "Dino", library_dir=tmp_path / "absent")
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
        lib = tmp_path / "catalog"
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
        assert MIN_GLYPH_WIDTH_FRACTION == pytest.approx(0.0)
        assert KERNING_WINDOW_FRACTION == pytest.approx(0.05)
        assert plt_font_renderer.KERNING_PENETRATION_SCALE == pytest.approx(1.0)
        assert plt_font_renderer.KERNING_MIN_GAP_FRACTION == pytest.approx(0.0)
        assert plt_font_renderer.FALLBACK_ADVANCE_FRACTION == pytest.approx(1.0)


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


class TestSmallCaps:
    """The ``"<base> SC"`` small-caps transform (PLT family).

    The synthetic library adds its OWN lowercase glyphs (``a``/``c``, half
    height) so a test can prove the variant draws the **uppercase** glyph
    downscaled, never the font's lowercase one. ``G`` descends below the
    baseline to pin the top-down anchor.
    """

    @pytest.fixture
    def sc_lib(self, tmp_path: Path) -> Path:
        """The synthetic library extended with lowercase + descender glyphs."""

        def _entry(
            hpgl: str,
            bbox: Tuple[float, float, float, float],
            left: Sequence[Tuple[float, float]],
            right: Sequence[Tuple[float, float]],
        ) -> Dict[str, Any]:
            return _v2_entry(hpgl, bbox, left, right)

        characters = dict(_SYNTH_CHARACTERS)
        # The font's own lowercase: half-height, visually distinct from the
        # full-height uppercase glyph the small caps must use instead.
        characters["a"] = _entry(
            "PU0,0;PD500,500;", (0, 0, 500, 500), [(0, 0), (500, 500)], [(0, 0), (500, 500)]
        )
        characters["c"] = _entry(
            "PU0,250;PD0,250;PD;AA250,250,360;",
            (0, 0, 500, 500),
            [(250, 0), (0, 250), (250, 500)],
            [(250, 0), (500, 250), (250, 500)],
        )
        # Uppercase G descends (y -0.5..0.5): pins the top-down anchor.
        characters["G"] = _entry(
            "PU0,-500;PD500,500;",
            (0, -500, 500, 500),
            [(0, -500), (500, 500)],
            [(0, -500), (500, 500)],
        )
        return _write_library(tmp_path, characters)

    def test_lowercase_uses_uppercase_glyph_downscaled(self, sc_lib: Path) -> None:
        """``a`` renders the full ``A`` glyph at 0.7 scale, not the font's ``a``."""
        block = _render("a", 1.0, sc_lib, font="Test SC", lowercase_scale=0.7)
        bounds = block.bounds()
        assert bounds is not None
        # A spans 1.0x1.0; the font's own 'a' spans 0.5x0.5. 0.7 proves the
        # uppercase glyph was drawn and shrunk.
        assert bounds[2] == pytest.approx(0.7, abs=1e-9)
        assert bounds[3] == pytest.approx(0.7, abs=1e-9)

    def test_scale_one_renders_full_height_capital(self, sc_lib: Path) -> None:
        """``sc_scale=1.0`` maps lowercase to the FULL-size capital."""
        scaled = _render("a", 1.0, sc_lib, font="Test SC", lowercase_scale=1.0)
        base = _render("A", 1.0, sc_lib)
        assert scaled.bounds() == pytest.approx(base.bounds())

    def test_transform_off_uses_the_fonts_own_lowercase(self, sc_lib: Path) -> None:
        """Without ``lowercase_scale`` the base font renders verbatim."""
        block = _render("a", 1.0, sc_lib)
        bounds = block.bounds()
        assert bounds is not None
        assert bounds[2] == pytest.approx(0.5, abs=1e-9)  # the font's own 'a'

    def test_top_down_anchor_keeps_the_bottom(self, sc_lib: Path) -> None:
        """The glyph's own lowest point stays put; its top descends."""
        base = _render("G", 1.0, sc_lib).bounds()
        shrunk = _render("g", 1.0, sc_lib, font="Test SC", lowercase_scale=0.7).bounds()
        assert base is not None and shrunk is not None
        assert base[1] == pytest.approx(-0.5, abs=1e-9)
        # Bottom unchanged, top 0.5 -> -0.5 + 1.0*0.7, width halved*0.7.
        assert shrunk[1] == pytest.approx(-0.5, abs=1e-9)
        assert shrunk[3] == pytest.approx(0.2, abs=1e-9)
        assert shrunk[2] == pytest.approx(0.35, abs=1e-9)

    def test_uppercase_and_caseless_glyphs_untouched(self, sc_lib: Path) -> None:
        """A line of capitals/digits keeps its exact base-font geometry."""
        sc = _render("AB", 1.0, sc_lib, font="Test SC", lowercase_scale=0.7)
        base = _render("AB", 1.0, sc_lib)
        assert sc.bounds() == pytest.approx(base.bounds())

    def test_kerning_uses_the_shrunk_silhouette(self, sc_lib: Path) -> None:
        """The small glyph advances like the small glyph it engraves.

        ``A``'s right silhouette is the diagonal ``x = y``, so shrinking it
        to 0.7 shrinks its penetration into the next glyph by the same 0.7:
        ``aB`` lands 0.3in earlier than the full-height ``AB``.
        """
        kwargs = {"cutter_diameter": 0.04, "character_spacing": 0.02}
        full = _render("AB", 1.0, sc_lib, **kwargs).bounds()
        small = _render("aB", 1.0, sc_lib, font="Test SC", lowercase_scale=0.7, **kwargs).bounds()
        assert full is not None and small is not None
        assert full[2] == pytest.approx(1.06, abs=1e-9)
        assert small[2] == pytest.approx(0.76, abs=1e-9)

    def test_arcs_stay_native_under_the_shrink(self, sc_lib: Path) -> None:
        """The uniform scale keeps the arc-native contract (no flattening)."""
        block = _render("c", 1.0, sc_lib, font="Test SC", lowercase_scale=0.7)
        assert _count_arcs(block) == 1
        assert "AA" in block.to_hpgl()

    def test_glyph_groups_keep_the_original_characters(self, sc_lib: Path) -> None:
        """The stroke partition stays keyed by the text, not the mapped glyphs."""
        _block, words, glyphs = render_text_line_plt_font_with_glyphs(
            "aB", 1.0, "Test SC", library_dir=sc_lib, lowercase_scale=0.7
        )
        assert [char for char, _ in glyphs] == ["a", "B"]
        assert [word for word, _ in words] == ["aB"]


class TestInlineSmallCaps:
    """The per-character ``lowercase_flags`` selection (the inline ``<sc>`` tag).

    The transform is :class:`TestSmallCaps`'s, narrowed to the flagged
    characters; the base font (no `` SC`` suffix) renders, so the flags are
    the only thing selecting the shrink.
    """

    @pytest.fixture
    def sc_lib(self, tmp_path: Path) -> Path:
        """The synthetic library extended with lowercase + descender glyphs."""

        def _entry(
            hpgl: str,
            bbox: Tuple[float, float, float, float],
            left: Sequence[Tuple[float, float]],
            right: Sequence[Tuple[float, float]],
        ) -> Dict[str, Any]:
            return _v2_entry(hpgl, bbox, left, right)

        characters = dict(_SYNTH_CHARACTERS)
        # The font's own lowercase: half-height, visually distinct from the
        # full-height uppercase glyph the small caps must use instead.
        characters["a"] = _entry(
            "PU0,0;PD500,500;", (0, 0, 500, 500), [(0, 0), (500, 500)], [(0, 0), (500, 500)]
        )
        characters["c"] = _entry(
            "PU0,250;PD0,250;PD;AA250,250,360;",
            (0, 0, 500, 500),
            [(250, 0), (0, 250), (250, 500)],
            [(250, 0), (500, 250), (250, 500)],
        )
        # Uppercase G descends (y -0.5..0.5): pins the top-down anchor.
        characters["G"] = _entry(
            "PU0,-500;PD500,500;",
            (0, -500, 500, 500),
            [(0, -500), (500, 500)],
            [(0, -500), (500, 500)],
        )
        return _write_library(tmp_path, characters)

    def test_flags_none_matches_the_whole_line_transform(self, sc_lib: Path) -> None:
        """``lowercase_flags=None`` is the font-level whole-line behaviour."""
        whole = _render("aB", 1.0, sc_lib, lowercase_scale=0.7)
        flagged = _render("aB", 1.0, sc_lib, lowercase_scale=0.7, lowercase_flags=(True, True))
        assert flagged.bounds() == pytest.approx(whole.bounds())

    def test_unflagged_glyphs_engrave_verbatim(self, sc_lib: Path) -> None:
        """All-False flags render the base font exactly (the tag's fast path)."""
        plain = _render("aB", 1.0, sc_lib)
        unflagged = _render("aB", 1.0, sc_lib, lowercase_scale=0.7, lowercase_flags=(False, False))
        assert unflagged.bounds() == pytest.approx(plain.bounds())

    def test_selection_is_per_character(self, sc_lib: Path) -> None:
        """Only the flagged lowercase glyph shrinks; its neighbours stay full.

        ``"aBa"`` has two lowercase ``a`` (each 0.5 wide) around a capital
        ``B``. Under the space-inclusive flags, ``(True, False, False)``
        shrinks only the first ``a`` and ``(False, False, True)`` only the
        last; ``(False,)*3`` shrinks none and ``(True,)*3`` shrinks both.
        Flagging one ``a`` lands strictly between the two extremes, and
        flagging the middle ``B`` (a capital) changes nothing.
        """

        def width(flags: Tuple[bool, bool, bool]) -> float:
            block = _render(
                "aBa",
                1.0,
                sc_lib,
                lowercase_scale=0.7,
                lowercase_flags=flags,
                cutter_diameter=0.02,
            )
            return block.bounds()[2]

        none = width((False, False, False))
        both = width((True, False, True))
        first = width((True, False, False))
        last = width((False, False, True))
        middle = width((False, True, False))
        assert none == pytest.approx(1.04, abs=1e-9)
        assert both == pytest.approx(1.44, abs=1e-9)
        # One flagged 'a' sits strictly between none and both.
        assert none < first < both
        assert none < last < both
        # Flagging the capital 'B' (uppercase) engraves verbatim.
        assert middle == pytest.approx(none, abs=1e-9)

    def test_flagged_glyph_uses_the_uppercase_source(self, sc_lib: Path) -> None:
        """A flagged ``a`` draws the shrunk ``A``, never the font's own ``a``."""
        block = _render("a", 1.0, sc_lib, lowercase_scale=0.7, lowercase_flags=(True,))
        bounds = block.bounds()
        assert bounds is not None
        # A spans 1.0x1.0; the font's own 'a' spans 0.5x0.5. 0.7 proves the
        # uppercase glyph was drawn and shrunk.
        assert bounds[2] == pytest.approx(0.7, abs=1e-9)
        assert bounds[3] == pytest.approx(0.7, abs=1e-9)

    def test_top_down_anchor_on_the_flagged_glyph(self, sc_lib: Path) -> None:
        """The flagged glyph's own lowest point stays put; its top descends."""
        base = _render("G", 1.0, sc_lib).bounds()
        shrunk = _render("g", 1.0, sc_lib, lowercase_scale=0.7, lowercase_flags=(True,)).bounds()
        assert base is not None and shrunk is not None
        assert shrunk[1] == pytest.approx(base[1], abs=1e-9)
        assert shrunk[3] == pytest.approx(0.2, abs=1e-9)

    def test_arcs_stay_native_under_the_flagged_shrink(self, sc_lib: Path) -> None:
        """The uniform scale keeps the arc-native contract (no flattening)."""
        block = _render("c", 1.0, sc_lib, lowercase_scale=0.7, lowercase_flags=(True,))
        assert _count_arcs(block) == 1
        assert "AA" in block.to_hpgl()

    def test_glyph_groups_keep_the_original_characters(self, sc_lib: Path) -> None:
        """The per-glyph partition is keyed by the text, not the mapped glyphs."""
        _block, _words, glyphs = render_text_line_plt_font_with_glyphs(
            "aB",
            1.0,
            "Test",
            library_dir=sc_lib,
            lowercase_scale=0.7,
            lowercase_flags=(True, False),
        )
        assert [char for char, _ in glyphs] == ["a", "B"]

    def test_space_index_counts_in_the_flags(self, sc_lib: Path) -> None:
        """The flags align with the text INCLUDING spaces (the italic rule).

        ``"a a"`` has its space at index 1. Under the space-inclusive rule
        ``(True, True, False)`` flags only the first ``a`` (one small
        capital), while ``(True, False, True)`` flags both (two). A
        space-skipping index would read the first tuple as "both flagged"
        and the two renders would agree.
        """
        one = _render(
            "a a",
            1.0,
            sc_lib,
            lowercase_scale=0.7,
            lowercase_flags=(True, True, False),
            cutter_diameter=0.02,
        )
        two = _render(
            "a a",
            1.0,
            sc_lib,
            lowercase_scale=0.7,
            lowercase_flags=(True, False, True),
            cutter_diameter=0.02,
        )
        plain = _render("a a", 1.0, sc_lib, cutter_diameter=0.02)
        assert one.bounds()[2] == pytest.approx(1.5, abs=1e-9)
        assert two.bounds()[2] == pytest.approx(1.7, abs=1e-9)
        assert plain.bounds()[2] == pytest.approx(1.3, abs=1e-9)

    def test_flag_length_mismatch_raises(self, sc_lib: Path) -> None:
        """A flags/text length mismatch is a loud ValueError."""
        with pytest.raises(ValueError, match="lowercase_flags length 1 != text length 2"):
            _render("aB", 1.0, sc_lib, lowercase_scale=0.7, lowercase_flags=(True,))

    def test_scale_one_with_flags_maps_without_shrinking(self, sc_lib: Path) -> None:
        """``lowercase_scale=1.0`` + flags: the uppercase map, no shrink."""
        mapped = _render("aB", 1.0, sc_lib, lowercase_scale=1.0, lowercase_flags=(True, False))
        caps = _render("AB", 1.0, sc_lib)
        assert mapped.bounds() == pytest.approx(caps.bounds())

    def test_bundled_font_small_caps(self) -> None:
        """The shipped Dino variant shrinks lowercase against the real library."""
        caps = render_text_line_plt_font("A", 0.5, "Dino")
        small = render_text_line_plt_font("a", 0.5, "Dino SC", lowercase_scale=0.7)
        caps_bounds = caps.bounds()
        small_bounds = small.bounds()
        assert caps_bounds is not None and small_bounds is not None
        assert small_bounds[3] == pytest.approx(caps_bounds[3] * 0.7, abs=1e-9)
        assert small_bounds[1] == pytest.approx(caps_bounds[1], abs=1e-9)

    def test_lowercase_only_line_reports_no_glyph_for_missing_capital(self, tmp_path: Path) -> None:
        """A missing UPPERCASE glyph is reported under the mapped character."""
        root = tmp_path / "nolib"
        root.mkdir()
        library = _write_library(root, {"a": _v2_entry("PU0,0;PD500,500;")})
        with pytest.raises(PltFontRenderError, match="has no glyph for character 'A'"):
            _render("a", 1.0, library, font="Test SC", lowercase_scale=0.7)


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
        block, groups, _glyphs = _render_line_block(
            _resolved_line("AB", "dino"), TextChunkMode.LINE
        )
        assert calls["text"] == "AB"
        assert calls["font_name"] == "Dino"  # canonical, not the raw input
        assert calls["target_height_inches"] == pytest.approx(0.27)
        assert calls["cutter_diameter"] == pytest.approx(0.03)
        assert calls["character_spacing"] == pytest.approx(0.02)
        # ResolvedTextLine defaults for the v2 typesetting fields.
        assert calls["space_width_fraction"] == pytest.approx(0.3)
        assert calls["min_glyph_width_fraction"] == pytest.approx(0.0)
        assert calls["kerning_window_fraction"] == pytest.approx(0.05)
        assert calls["kerning_penetration_scale"] == pytest.approx(1.0)
        assert calls["kerning_min_gap_fraction"] == pytest.approx(0.0)
        assert calls["fallback_advance_fraction"] == pytest.approx(1.0)
        assert block.strokes == _triangle_block().strokes
        assert groups is None  # line mode never requests word groups

    def test_plt_word_mode_returns_groups(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """WORD chunk mode forwards the renderer's exact stroke groups."""

        def fake_with_words(
            text: str, **kwargs: object
        ) -> tuple[TextBlock, list[tuple[str, list[int]]]]:
            return _triangle_block(), [("AB", [0])]

        monkeypatch.setattr(label_renderer, "render_text_line_plt_font_with_words", fake_with_words)
        _block, groups, _glyphs = _render_line_block(
            _resolved_line("AB", "Dino"), TextChunkMode.WORD
        )
        assert groups == [("AB", [0])]

    def test_small_caps_variant_threads_lowercase_scale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An ``SC`` font forwards the cascaded ``sc_scale`` to the renderer."""
        calls: dict[str, object] = {}

        def fake_render(text: str, **kwargs: object) -> TextBlock:
            calls.update(kwargs)
            return _triangle_block()

        monkeypatch.setattr(label_renderer, "render_text_line_plt_font", fake_render)
        line = ResolvedTextLine(
            text="Ab",
            nominal_text_height=0.3,
            toolpath_text_height=0.27,
            cutter_diameter=0.03,
            character_spacing=0.02,
            line_spacing=0.0,
            font="Dino SC",
            sc_scale=0.55,
        )
        _render_line_block(line, TextChunkMode.LINE)
        assert calls["font_name"] == "Dino SC"
        assert calls["lowercase_scale"] == pytest.approx(0.55)

    def test_base_font_never_receives_lowercase_scale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-SC font keeps the renderer default (bit-identical legacy call)."""
        calls: dict[str, object] = {}

        def fake_render(text: str, **kwargs: object) -> TextBlock:
            calls.update(kwargs)
            return _triangle_block()

        monkeypatch.setattr(label_renderer, "render_text_line_plt_font", fake_render)
        line = ResolvedTextLine(
            text="Ab",
            nominal_text_height=0.3,
            toolpath_text_height=0.27,
            cutter_diameter=0.03,
            character_spacing=0.02,
            line_spacing=0.0,
            font="Dino",
            sc_scale=0.55,
        )
        _render_line_block(line, TextChunkMode.LINE)
        assert "lowercase_scale" not in calls

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
        more native ``AA`` commands survive end-to-end. Re-pinned again for
        the multi-stroke library regen: the shipped Dino/Jhanuni sheets are
        now the 0.67/0.6 ``_S1`` engravings, so the demo fonts' geometry (and
        the compression that flattens arcs) shifted.

        Re-pinned for alphanumeric cap-line enforcement (``enforce_cap_line``,
        default true): the shipped sheets' digits and round/pointed capitals
        overshoot the capline, and the clamp's **vertical-only** scale cannot
        keep ``AA`` arcs native (a non-uniform scale maps a circular arc to an
        ellipse), so every clamped glyph flattens at the chord tolerance --
        the ``TextBlock.scale_y`` policy. The counts drop accordingly; a label
        of exact-cap glyphs (or an ``enforce_cap_line: false`` line) keeps its
        arcs.
        """
        rendered = self._rendered()
        counts = {key: content.plt_content.count("AA") for key, (_rl, content) in rendered.items()}
        assert counts["dino_banner"] == 15
        assert counts["mixed_tag"] == 92
        assert counts["ttf_card"] == 0


class TestDerivedGlyphRendering:
    """The derived Unicode glyphs of the shipped library typeset like the rest."""

    def test_derived_string_renders(self) -> None:
        """A mixed ASCII/derived string renders non-empty and upright."""
        block = render_text_line_plt_font("A\u2013B\u2014C", 0.5, "Dino")
        bounds = block.bounds()
        assert bounds is not None
        min_x, _min_y, max_x, max_y = bounds
        assert math.isclose(min_x, 0.0, abs_tol=1e-9)
        assert max_x > min_x
        assert max_y > 0.0

    def test_en_dash_is_wider_than_hyphen(self) -> None:
        """The derived en dash advances EN_DASH_SCALE x the hyphen's width."""
        hyphen = render_text_line_plt_font("-", 0.5, "Dino").bounds()
        dash = render_text_line_plt_font("\u2013", 0.5, "Dino").bounds()
        assert hyphen is not None and dash is not None
        hyphen_width = hyphen[2] - hyphen[0]
        dash_width = dash[2] - dash[0]
        assert dash_width == pytest.approx(_gt.EN_DASH_SCALE * hyphen_width, rel=1e-6)

    def test_bullet_sits_on_the_midline(self) -> None:
        """The derived bullet is centred on half the cap height."""
        block = render_text_line_plt_font("\u2022", 0.5, "Dino")
        bounds = block.bounds()
        assert bounds is not None
        centre = (bounds[1] + bounds[3]) / 2.0
        assert centre == pytest.approx(0.25, rel=1e-3)  # midline of a 0.5 cap

    def test_infinity_keeps_native_arcs(self) -> None:
        """The rotated eight keeps its AA geometry end-to-end."""
        block = render_text_line_plt_font("\u221e", 0.5, "Dino")
        assert _count_arcs(block) > 0
        assert "AA" in block.to_hpgl()

    def test_inverted_question_lands_on_the_baseline(self) -> None:
        """``\u00bf`` sits on the baseline like an upright glyph."""
        block = render_text_line_plt_font("\u00bf", 0.5, "Dino")
        bounds = block.bounds()
        assert bounds is not None
        assert bounds[1] == pytest.approx(0.0, abs=1e-6)

    def test_inverted_exclamation_descends(self) -> None:
        """``\u00a1`` hangs below the baseline by the tuned descent."""
        block = render_text_line_plt_font("\u00a1", 0.5, "Dino")
        bounds = block.bounds()
        assert bounds is not None
        # The recipe mirrors ! (0..cap) in the baseline then translates by
        # -INVERTED_EXCLAMATION_DESCENT * cap, so the bottom sits at
        # -(1 + descent) * cap in inches (cap == the rendered text height).
        expected_bottom = -0.5 * (1.0 + _gt.INVERTED_EXCLAMATION_DESCENT)
        # Dino's ``!`` measures 1000.58 units (not exactly the 1000-unit cap),
        # so the rendered bottom drifts from the ideal by that ratio.
        assert bounds[1] == pytest.approx(expected_bottom, rel=1e-2)
        assert bounds[1] < 0.0

    def test_derived_glyphs_kern_against_letters(self) -> None:
        """Envelope kerning accepts derived silhouettes (finite advances)."""
        block = render_text_line_plt_font("T\u2192A", 0.5, "Dino", cutter_diameter=0.04)
        bounds = block.bounds()
        assert bounds is not None
        assert bounds[2] > bounds[0]
        assert bounds[2] < 3.0  # sane advance, no runaway fallback

    def test_all_derived_characters_render(self) -> None:
        """Every derived key in the shipped library renders without raising."""
        catalog_dir = Path(__file__).resolve().parents[1] / "fonts" / "VP-EL-fonts" / "catalog"
        dino = json.loads((catalog_dir / "Dino.json").read_text(encoding="utf-8"))
        derived = [char for char in dino["characters"] if ord(char) > 0x7E]
        assert len(derived) == 17
        for char in derived:
            block = render_text_line_plt_font(f"A{char}A", 0.4, "Dino")
            assert block.strokes, f"derived {char!r} rendered nothing"


class TestItalicShear:
    """The ``<i>`` shear transform (PLT family).

    ``A`` is the diagonal ``(0,0)-(1,1)`` (its top leans right under a
    positive shear), ``B`` is the zero-width vertical at ``x = 0``, ``C`` is
    a full circle arc (the arc-flattening pin) and ``g`` descends to
    ``y = -0.5`` (the sub-baseline lean pin). The shear is
    ``x' = x + y * tan`` about the baseline.
    """

    _TAN = 0.5

    @pytest.fixture
    def sc_lib(self, tmp_path: Path) -> Path:
        """The synthetic library extended with lowercase + descender glyphs.

        Mirrors ``TestSmallCaps.sc_lib`` (a class-scoped fixture): ``a`` is
        the font's own half-height lowercase and ``G`` descends, so a
        small-caps + shear test can pin the uppercase source and the
        baseline anchoring.
        """
        characters = dict(_SYNTH_CHARACTERS)
        characters["a"] = _v2_entry(
            "PU0,0;PD500,500;", (0, 0, 500, 500), [(0, 0), (500, 500)], [(0, 0), (500, 500)]
        )
        characters["G"] = _v2_entry(
            "PU0,-500;PD500,500;",
            (0, -500, 500, 500),
            [(0, -500), (500, 500)],
            [(0, -500), (500, 500)],
        )
        return _write_library(tmp_path, characters)

    def test_glyph_shears_about_the_baseline(self, synth_lib: Path) -> None:
        """The baseline stays fixed, the top leans right, height untouched."""
        plain = _render("A", 1.0, synth_lib).bounds()
        italic = _render("A", 1.0, synth_lib, italic_tan=self._TAN).bounds()
        assert plain is not None and italic is not None
        assert plain == pytest.approx((0.0, 0.0, 1.0, 1.0))
        # x' = 1 + 1*0.5 at the cap line; y untouched.
        assert italic == pytest.approx((0.0, 0.0, 1.5, 1.0))

    def test_sub_baseline_ink_leans_left(self, synth_lib: Path) -> None:
        """A descender (negative y) leans the other way (the shear is linear)."""
        plain = _render("g", 1.0, synth_lib).bounds()
        italic = _render("g", 1.0, synth_lib, italic_tan=self._TAN).bounds()
        assert plain is not None and italic is not None
        assert plain == pytest.approx((0.0, -0.5, 0.5, 0.5))
        assert italic == pytest.approx((-0.25, -0.5, 0.75, 0.5))

    def test_zero_tan_is_bit_identical(self, synth_lib: Path) -> None:
        """``tan = 0`` (italic_angle 0) renders the untouched geometry."""
        plain = _render("AB", 1.0, synth_lib)
        upright = _render("AB", 1.0, synth_lib, italic_tan=0.0)
        assert upright.to_hpgl() == plain.to_hpgl()

    def test_flags_select_the_glyphs(self, synth_lib: Path) -> None:
        """Only the flagged glyphs shear (per-character markup runs)."""
        kwargs = {"cutter_diameter": 0.04, "character_spacing": 0.02}
        plain = _render("AB", 1.0, synth_lib, **kwargs).bounds()
        first = _render(
            "AB", 1.0, synth_lib, italic_tan=self._TAN, italic_flags=(True, False), **kwargs
        ).bounds()
        both = _render("AB", 1.0, synth_lib, italic_tan=self._TAN, **kwargs).bounds()
        assert plain is not None and first is not None and both is not None
        # A's sheared right silhouette (x = 0.5 + y*0.5 at the top) penetrates
        # B by 1.5, so B's origin lands 0.5in later than the plain pair.
        assert plain == pytest.approx((0.0, 0.0, 1.06, 1.0))
        assert first == pytest.approx((0.0, 0.0, 1.56, 1.0))
        assert both is not None and both[2] == pytest.approx(1.56, abs=1e-9)

    def test_kerning_uses_the_slanted_silhouette(self, synth_lib: Path) -> None:
        """Shearing BOTH glyphs keeps the pair's advance geometrically exact.

        A's sheared right edge and B's sheared left edge both shift by
        ``y * tan``, so the penetration (and the advance) is the plain pair's
        and the line widens by exactly one overhang, not two.
        """
        kwargs = {"cutter_diameter": 0.04, "character_spacing": 0.02}
        plain = _render("AB", 1.0, synth_lib, **kwargs).bounds()
        italic = _render("AB", 1.0, synth_lib, italic_tan=self._TAN, **kwargs).bounds()
        assert plain is not None and italic is not None
        assert italic[2] - plain[2] == pytest.approx(1.0 * self._TAN, abs=1e-9)

    def test_arcs_flatten_under_the_shear(self, synth_lib: Path) -> None:
        """A sheared circular arc is a rotated ellipse: it must flatten."""
        plain = _render("C", 1.0, synth_lib)
        italic = _render("C", 1.0, synth_lib, italic_tan=self._TAN)
        assert _count_arcs(plain) == 1
        assert _count_arcs(italic) == 0
        assert _count_lines(italic) > _count_lines(plain)

    def test_stroke_count_is_preserved_for_polylines(self, synth_lib: Path) -> None:
        """The per-glyph stroke partition survives (the glyph sweep stays valid)."""
        plain = _render("AB", 1.0, synth_lib)
        italic = _render("AB", 1.0, synth_lib, italic_tan=self._TAN)
        assert len(italic.strokes) == len(plain.strokes)

    def test_flags_length_mismatch_is_rejected(self, synth_lib: Path) -> None:
        """A flag map that does not cover the text is a programming error."""
        with pytest.raises(ValueError, match="italic_flags length"):
            render_text_line_plt_font(
                "AB", 1.0, "Test", library_dir=synth_lib, italic_tan=0.5, italic_flags=(True,)
            )

    def test_shear_composes_with_small_caps(self, sc_lib: Path) -> None:
        """Small caps shrinks first, then the small glyph shears (SC + <i>)."""
        kwargs = {"font": "Test SC", "lowercase_scale": 0.7, "italic_tan": self._TAN}
        small_plain = _render("a", 1.0, sc_lib, font="Test SC", lowercase_scale=0.7).bounds()
        small_italic = _render("a", 1.0, sc_lib, **kwargs).bounds()  # type: ignore[arg-type]
        assert small_plain is not None and small_italic is not None
        # The 0.7 capital's top (y = 0.7) leans by 0.7 * tan.
        assert small_italic[2] == pytest.approx(0.7 + 0.7 * self._TAN, abs=1e-9)
        assert small_italic[3] == pytest.approx(0.7, abs=1e-9)

    def test_shear_composes_with_a_descender_glyph(self, sc_lib: Path) -> None:
        """SC + shear on a descending base glyph anchors on y = 0, not its bottom."""
        italic = _render(
            "g", 1.0, sc_lib, font="Test SC", lowercase_scale=0.7, italic_tan=self._TAN
        ).bounds()
        assert italic is not None
        # G spans -0.5..0.5; the 0.7 shrink (top-down) spans -0.5..0.2. The
        # shear about y = 0 leans the top by 0.2*tan and the bottom-left point
        # (x = 0 at y = -0.5) left by 0.5*tan: x' range = [-0.25, 0.45].
        assert italic[1] == pytest.approx(-0.5, abs=1e-9)
        assert italic[3] == pytest.approx(0.2, abs=1e-9)
        assert italic[0] == pytest.approx(-0.25, abs=1e-9)
        assert italic[2] == pytest.approx(0.45, abs=1e-9)


class TestBoldMarkup:
    """The ``<b>`` per-character cutter diameters + baseline-anchored glyph scale.

    ``A`` is the diagonal ``(0,0)-(1,1)`` (bbox ``(0,0,1,1)`` at height 1.0),
    ``B`` is the zero-width vertical at ``x = 0`` (so the pair's advance is
    exactly the kerning offset plus the clearance), ``C`` is a full circle arc
    (the arc-native pin) and ``g`` descends to ``y = -0.5`` (the baseline
    anchoring pin). The bold scale is ``x' = x*f, y' = y*f`` about the baseline.
    """

    def test_glyph_scale_shrinks_about_the_baseline(self, synth_lib: Path) -> None:
        """A bold glyph scales uniformly toward the baseline (top descends)."""
        plain = _render("A", 1.0, synth_lib).bounds()
        bold = _render("A", 1.0, synth_lib, glyph_scales=(0.5,)).bounds()
        assert plain is not None and bold is not None
        assert plain == pytest.approx((0.0, 0.0, 1.0, 1.0))
        # x' = 1*0.5, y' = 1*0.5; the baseline (y=0) stays fixed.
        assert bold == pytest.approx((0.0, 0.0, 0.5, 0.5))

    def test_descender_shrinks_toward_the_baseline(self, synth_lib: Path) -> None:
        """A sub-baseline overshoot shrinks with the rest of the glyph."""
        plain = _render("g", 1.0, synth_lib).bounds()
        bold = _render("g", 1.0, synth_lib, glyph_scales=(0.5,)).bounds()
        assert plain is not None and bold is not None
        assert plain == pytest.approx((0.0, -0.5, 0.5, 0.5))
        assert bold == pytest.approx((0.0, -0.25, 0.25, 0.25))

    def test_scale_one_is_bit_identical(self, synth_lib: Path) -> None:
        """``glyph_scales = 1.0`` renders the untouched geometry."""
        plain = _render("AB", 1.0, synth_lib)
        bold = _render("AB", 1.0, synth_lib, glyph_scales=(1.0, 1.0))
        assert bold.to_hpgl() == plain.to_hpgl()

    def test_flags_select_the_glyphs(self, synth_lib: Path) -> None:
        """Only the scaled glyph shrinks (per-character bold runs)."""
        plain = _render("AB", 1.0, synth_lib).bounds()
        first = _render("AB", 1.0, synth_lib, glyph_scales=(0.5, 1.0)).bounds()
        assert plain is not None and first is not None
        # A shrinks to width 0.5; the A->B pair's advance is unchanged (the
        # clearance is the same), so B lands at the plain origin and the line
        # is narrower only by A's shrink.
        assert plain[2] > first[2]

    def test_per_pair_clearance_uses_the_two_cutters(self, synth_lib: Path) -> None:
        """A bold neighbour widens the pair's clearance to 0.5*(dL+dR)."""
        kwargs = {"character_spacing": 0.02}
        plain = _render("AB", 1.0, synth_lib, cutter_diameter=0.04, **kwargs).bounds()
        bold = _render(
            "AB",
            1.0,
            synth_lib,
            cutter_diameter=0.04,
            cutter_diameters=(0.04, 0.09),
            **kwargs,
        ).bounds()
        assert plain is not None and bold is not None
        # Plain clearance 0.04+0.02 = 0.06 -> advance 1.06; bold pair
        # clearance 0.5*(0.04+0.09)+0.02 = 0.085 -> advance 1.085.
        assert plain[2] == pytest.approx(1.06, abs=1e-9)
        assert bold[2] == pytest.approx(1.085, abs=1e-9)

    def test_arcs_stay_native_under_the_scale(self, synth_lib: Path) -> None:
        """A uniform scale keeps ``AA`` arcs native (a scaled circle is a circle)."""
        plain = _render("C", 1.0, synth_lib)
        bold = _render("C", 1.0, synth_lib, glyph_scales=(0.7,))
        assert _count_arcs(plain) == 1
        assert _count_arcs(bold) == 1

    def test_stroke_count_is_preserved(self, synth_lib: Path) -> None:
        """The per-glyph stroke partition survives (the glyph sweep stays valid)."""
        plain = _render("AB", 1.0, synth_lib)
        bold = _render("AB", 1.0, synth_lib, glyph_scales=(0.8, 0.8))
        assert len(bold.strokes) == len(plain.strokes)

    def test_scale_length_mismatch_is_rejected(self, synth_lib: Path) -> None:
        """A glyph_scales length that is not the text length raises ValueError."""
        with pytest.raises(ValueError, match="glyph_scales length"):
            _render("AB", 1.0, synth_lib, glyph_scales=(0.5,))

    def test_cutter_diameters_length_mismatch_is_rejected(self, synth_lib: Path) -> None:
        """A cutter_diameters length that is not the text length raises ValueError."""
        with pytest.raises(ValueError, match="cutter_diameters length"):
            _render("AB", 1.0, synth_lib, cutter_diameters=(0.04,))

    def test_none_kwargs_are_bit_identical(self, synth_lib: Path) -> None:
        """Omitting the bold kwargs keeps the historical render."""
        kwargs = {"cutter_diameter": 0.04, "character_spacing": 0.02}
        legacy = _render("AB", 1.0, synth_lib, **kwargs)
        explicit = _render("AB", 1.0, synth_lib, cutter_diameters=None, glyph_scales=None, **kwargs)
        assert explicit.to_hpgl() == legacy.to_hpgl()

    def test_bold_scales_the_silhouette_the_kerning_measures(self, synth_lib: Path) -> None:
        """A scaled left glyph's shrunken silhouette penetrates its neighbour less.

        ``R`` sits at design x = 0.95..1.0 (a right-edge sliver). Scaling the
        left glyph about the baseline scales its x too, so a smaller left glyph
        leaves the pair's advance driven by the (unchanged) clearance, not the
        penetration -- the scaled silhouette is what kerns.
        """
        kwargs = {"character_spacing": 0.0}
        plain = _render("RA", 1.0, synth_lib, cutter_diameter=0.0, **kwargs).bounds()
        bold = _render(
            "RA", 1.0, synth_lib, cutter_diameter=0.0, glyph_scales=(0.5, 1.0), **kwargs
        ).bounds()
        assert plain is not None and bold is not None
        # R's right edge halves (1.0 -> 0.5), so A's origin lands earlier.
        assert bold[2] < plain[2]


class TestBaselineShiftMarkup:
    """The ``<sup>``/``<sub>`` per-character baseline shift (a pure translate).

    ``A`` is the diagonal ``(0,0)-(1,1)``, ``g`` descends to ``y = -0.5`` and
    ``C`` is a full circle arc. The shift is applied at placement, so the
    glyph rides above (positive) or below (negative) the baseline with its
    shape untouched, the advance is unchanged, and ``AA`` arcs stay native.
    """

    def test_shift_moves_the_glyph_up(self, synth_lib: Path) -> None:
        """A positive shift lifts the whole glyph; the shape is untouched."""
        plain = _render("A", 1.0, synth_lib).bounds()
        raised = _render("A", 1.0, synth_lib, baseline_shifts=(0.5,)).bounds()
        assert plain is not None and raised is not None
        assert plain == pytest.approx((0.0, 0.0, 1.0, 1.0))
        assert raised == pytest.approx((0.0, 0.5, 1.0, 1.5))

    def test_shift_moves_the_glyph_down(self, synth_lib: Path) -> None:
        """A negative shift drops the glyph (the subscript direction)."""
        plain = _render("A", 1.0, synth_lib).bounds()
        dropped = _render("A", 1.0, synth_lib, baseline_shifts=(-0.5,)).bounds()
        assert plain is not None and dropped is not None
        assert dropped == pytest.approx((0.0, -0.5, 1.0, 0.5))

    def test_shift_composes_with_the_glyph_scale(self, synth_lib: Path) -> None:
        """The bare <sup> shape: scaled about the baseline, then shifted."""
        shifted = _render("A", 1.0, synth_lib, glyph_scales=(0.5,), baseline_shifts=(0.5,)).bounds()
        assert shifted is not None
        # Scale first (0..1 -> 0..0.5), then the translate (+0.5).
        assert shifted == pytest.approx((0.0, 0.5, 0.5, 1.0))

    def test_shift_leaves_a_constant_silhouette_advance_alone(self, synth_lib: Path) -> None:
        """A shifted glyph with a constant-x silhouette keeps the pair advance.

        ``B`` is a zero-width vertical line (its right/left silhouettes sit at
        a constant x), so a vertical shift changes no penetration: the pair's
        advance is exactly the unshifted pair's. This is a property of the
        glyph, not of kerning ignoring the shift -- a shifted *shaped* glyph
        (see ``test_shift_narrows_a_pair``) does change the advance.
        """
        kwargs = {"character_spacing": 0.02, "cutter_diameter": 0.04}
        plain = _render("AB", 1.0, synth_lib, **kwargs).bounds()
        shifted = _render("AB", 1.0, synth_lib, baseline_shifts=(0.0, 0.5), **kwargs).bounds()
        assert plain is not None and shifted is not None
        assert shifted[2] == pytest.approx(plain[2])

    def test_shift_narrows_a_pair(self, synth_lib: Path) -> None:
        """A raised glyph kerns at its true height, narrowing the pair.

        ``C`` is a circle radius 0.5 centred (0.5, 0.5). Two circles side by
        side advance 1.0 (they touch at x = 1.0). Raising the second by 0.5
        clears the first's widest bulge: the pair now overlaps only where the
        first tapers, so the advance drops to 0.5 -- the reported symptom (a
        raised superscript engraved with too much spacing).
        """
        plain = _render("CC", 1.0, synth_lib).bounds()
        raised = _render("CC", 1.0, synth_lib, baseline_shifts=(0.0, 0.5)).bounds()
        assert plain is not None and raised is not None
        assert plain[2] == pytest.approx(2.0)
        assert raised[2] == pytest.approx(1.5)

    def test_shift_widens_a_pair(self, synth_lib: Path) -> None:
        """A glyph raised past its neighbour's top overlaps only a sliver.

        ``A`` is the diagonal ``(0,0)->(1,1)``. Two ``A``s advance 0 (their
        silhouettes coincide). Raising the second by 1.0 leaves a single
        overlap point at y = 1 where the second's left edge is its bottom
        (x = 0), so the advance grows to 1.0.
        """
        plain = _render("AA", 1.0, synth_lib).bounds()
        raised = _render("AA", 1.0, synth_lib, baseline_shifts=(0.0, 1.0)).bounds()
        assert plain is not None and raised is not None
        assert plain[2] == pytest.approx(1.0)
        assert raised[2] == pytest.approx(2.0)

    def test_shift_disjoint_pair_falls_to_the_fallback(self, synth_lib: Path) -> None:
        """A shift that separates the boxes uses the fallback, not a phantom overlap.

        ``A`` shifted 1.5 puts the second box entirely above the first (no
        Y-overlap), so the pair falls to the bounding-box-width fallback
        ``max(left_width, min_glyph_width_fraction) * fallback_fraction`` = the left
        glyph's full width (1.0 at the default fallback fraction).
        """
        raised = _render("AA", 1.0, synth_lib, baseline_shifts=(0.0, 1.5)).bounds()
        assert raised is not None
        assert raised[2] == pytest.approx(2.0)

    def test_shift_composes_with_the_glyph_scale_in_kerning(self, synth_lib: Path) -> None:
        """The bare <sup> shape: scaled, then shifted, and kerned as that shape.

        ``A`` scaled 0.5 about the baseline (0..1 -> 0..0.5) then shifted 0.5
        spans y in [0.5, 1.0]. A following ``A`` at full height overlaps it in
        [0.5, 1.0]; the scaled glyph's right envelope at y is x = 2y - 1 (the
        scaled diagonal), so the pair's advance reflects the scaled+shifted
        silhouette (the same geometry the placement engraves).
        """
        shifted = _render(
            "AA", 1.0, synth_lib, glyph_scales=(1.0, 0.5), baseline_shifts=(0.0, 0.5)
        ).bounds()
        assert shifted is not None
        # First A: origin 0, spans 0..1. Second A: scaled 0.5 -> box 0..0.5,
        # shifted +0.5 -> 0.5..1.0. Overlap with the first [0.5, 1.0]; at y the
        # second's left envelope is x = 2y - 1, the first's right is x = y, so
        # penetration = y - (2y - 1) = 1 - y, maximised at y = 0.5 -> 0.5.
        # The second glyph's origin lands at 0.5 and it spans 0.5, so the
        # line's total width is 1.0.
        assert shifted[2] == pytest.approx(1.0)

    def test_zero_shift_is_bit_identical(self, synth_lib: Path) -> None:
        """``baseline_shifts = 0.0`` renders the untouched geometry."""
        plain = _render("AB", 1.0, synth_lib)
        shifted = _render("AB", 1.0, synth_lib, baseline_shifts=(0.0, 0.0))
        assert shifted.to_hpgl() == plain.to_hpgl()

    def test_none_is_bit_identical(self, synth_lib: Path) -> None:
        """Omitting the kwarg keeps the historical render."""
        kwargs = {"cutter_diameter": 0.04, "character_spacing": 0.02}
        legacy = _render("AB", 1.0, synth_lib, **kwargs)
        explicit = _render("AB", 1.0, synth_lib, baseline_shifts=None, **kwargs)
        assert explicit.to_hpgl() == legacy.to_hpgl()

    def test_arcs_stay_native_under_the_shift(self, synth_lib: Path) -> None:
        """A pure translate keeps ``AA`` arcs native."""
        plain = _render("C", 1.0, synth_lib)
        shifted = _render("C", 1.0, synth_lib, baseline_shifts=(0.25,))
        assert _count_arcs(plain) == 1
        assert _count_arcs(shifted) == 1

    def test_stroke_count_is_preserved(self, synth_lib: Path) -> None:
        """The per-glyph stroke partition survives (the glyph sweep stays valid)."""
        plain = _render("AB", 1.0, synth_lib)
        shifted = _render("AB", 1.0, synth_lib, baseline_shifts=(0.5, -0.5))
        assert len(shifted.strokes) == len(plain.strokes)

    def test_pen_up_target_shifts_with_the_glyph(self, synth_lib: Path) -> None:
        """A shifted glyph's plunge stays on the shifted geometry."""
        shifted = _render("A", 1.0, synth_lib, baseline_shifts=(0.5,))
        plain = _render("A", 1.0, synth_lib)
        assert shifted.strokes[0].pen_up == pytest.approx(plain.strokes[0].pen_up + 0.5j)

    def test_shift_length_mismatch_is_rejected(self, synth_lib: Path) -> None:
        """A baseline_shifts length that is not the text length raises ValueError."""
        with pytest.raises(ValueError, match="baseline_shifts length"):
            _render("AB", 1.0, synth_lib, baseline_shifts=(0.5,))


def _write_second_font(catalog: Path, name: str, characters: Dict[str, Dict[str, Any]]) -> Path:
    """Add a second v2 font file to an existing catalog directory.

    Args:
        catalog: The catalog directory (from :func:`_write_library`).
        name: The font's ``font_name`` key.
        characters: Character -> v2 entry mapping.

    Returns:
        The catalog directory.
    """
    (catalog / f"{name}.json").write_text(
        json.dumps(
            {
                "font_name": name,
                "reference_char": "A",
                "declared_height_in": 1.0,
                "reference_char_height_in": 1.0,
                "normalized_ref_height": 1.0,
                "characters": characters,
            }
        ),
        encoding="utf-8",
    )
    return catalog


class TestFontNames:
    """The per-character ``font_names`` selection (the inline ``<f:...>`` tag).

    Every v2 library shares the design frame (baseline y=0, reference char
    exactly 1000 units), so a character fetched from a second font's catalog
    entry kerns on the envelopes exactly like a same-font pair.
    """

    @pytest.fixture
    def two_fonts(self, tmp_path: Path) -> Path:
        """Two fonts: ``Test`` (full-height A) + ``Half`` (half-height A)."""
        catalog = _write_library(tmp_path, dict(_SYNTH_CHARACTERS))
        # The second font's 'A' is a half-height diagonal (0.5 x 0.5 in), so a
        # run in it is visibly shorter than the base font's full-height 'A'.
        _write_second_font(
            catalog,
            "Half",
            {
                "A": _v2_entry(
                    "PU0,0;PD500,500;",
                    (0, 0, 500, 500),
                    [(0, 0), (500, 500)],
                    [(0, 0), (500, 500)],
                ),
                "B": _SYNTH_CHARACTERS["B"],
            },
        )
        return catalog

    def test_font_names_length_mismatch_raises(self, two_fonts: Path) -> None:
        """A font_names array shorter than the text is a hard ValueError."""
        with pytest.raises(ValueError, match="font_names length"):
            _render("AB", 1.0, two_fonts, font_names=(None,))

    def test_all_none_matches_the_base_font(self, two_fonts: Path) -> None:
        """All-None font_names render the base font exactly (the fast path)."""
        plain = _render("AB", 1.0, two_fonts, cutter_diameter=0.02)
        flagged = _render("AB", 1.0, two_fonts, cutter_diameter=0.02, font_names=(None, None))
        assert flagged.bounds() == pytest.approx(plain.bounds())

    def test_selection_is_per_character(self, two_fonts: Path) -> None:
        """Only the named character fetches its geometry from the run font.

        ``"AA"`` in the base font is two full-height glyphs; flagging the
        second ``A`` to ``Half`` shrinks only that glyph, so the line is
        strictly shorter than the all-base render and taller than the
        all-Half render.
        """

        def width(font_names: Tuple[Optional[str], Optional[str]]) -> float:
            block = _render("AA", 1.0, two_fonts, cutter_diameter=0.02, font_names=font_names)
            return block.bounds()[2]

        base = width((None, None))
        second = width((None, "Half"))
        first = width(("Half", None))
        both = width(("Half", "Half"))
        # The parallel diagonals kern to offset 0 (full penetration), so the
        # total width is the leading glyph's origin + the trailing glyph's own
        # width. Shrinking the TRAILING glyph (second) narrows the line; the
        # leading glyph's width never sets the right edge, so leading with the
        # half glyph (first) keeps the base width; both-half is the narrowest.
        assert second < base
        assert first == pytest.approx(base, abs=1e-9)
        assert both < second

    def test_run_font_height_follows_the_frame(self, two_fonts: Path) -> None:
        """A run glyph scales by its own font's frame (half-height A -> 0.5).

        The base font's 'A' is 1.0 tall at target height 1.0; the ``Half``
        font's 'A' is 0.5 tall (its reference char is the full-height 'A', so
        the half-height glyph renders at half the target).
        """
        block = _render("A", 1.0, two_fonts, font_names=("Half",))
        bounds = block.bounds()
        assert bounds is not None
        assert bounds[3] == pytest.approx(0.5, abs=1e-9)

    def test_glyph_groups_are_exact_across_the_switch(self, two_fonts: Path) -> None:
        """The per-glyph partition stays a 1:1 cover across a font switch."""
        _block, _words, glyphs = render_text_line_plt_font_with_glyphs(
            "AB", 1.0, "Test", library_dir=two_fonts, font_names=(None, "Half")
        )
        assert [char for char, _ in glyphs] == ["A", "B"]


_CAP_ENFORCE_CHARACTERS: Dict[str, Dict[str, Any]] = {
    # Overshooting round cap: 1.0 wide, top 5% above the capline (1050).
    "O": _v2_entry(
        "PU0,0;PD1000,0;PD1000,1050;PD0,1050;PD0,0;",
        (0, 0, 1000, 1050),
        [(0, 0), (0, 1050)],
        [(1000, 0), (1000, 1050)],
    ),
    # Exact-cap flat cap: top exactly 1000, must never move.
    "H": _v2_entry(
        "PU0,0;PD1000,0;PD1000,1000;PD0,1000;PD0,0;",
        (0, 0, 1000, 1000),
        [(0, 0), (0, 1000)],
        [(1000, 0), (1000, 1000)],
    ),
    # Overshooting punctuation: never clamped (the alphanumeric gate).
    "!": _v2_entry(
        "PU0,0;PD100,0;PD100,1100;PD0,1100;PD0,0;",
        (0, 0, 100, 1100),
        [(0, 0), (0, 1100)],
        [(100, 0), (100, 1100)],
    ),
    # Descender + overshoot: bottom -500, top 1100.
    "g": _v2_entry(
        "PU0,-500;PD500,-500;PD500,1100;PD0,1100;PD0,-500;",
        (0, -500, 500, 1100),
        [(0, -500), (0, 1100)],
        [(500, -500), (500, 1100)],
    ),
    # Small lowercase for the SC ordering pin.
    "o": _v2_entry(
        "PU0,0;PD500,0;PD500,500;PD0,500;PD0,0;",
        (0, 0, 500, 500),
        [(0, 0), (0, 500)],
        [(500, 0), (500, 500)],
    ),
    # Tapered glyph: its deepest envelope penetration sits at the top, so
    # clamping the top measurably narrows a "VV" pair.
    "V": _v2_entry(
        "PU0,1050;PD500,0;PD1000,1050;",
        (0, 0, 1000, 1050),
        [(0, 1050), (500, 0)],
        [(500, 0), (1000, 1050)],
    ),
    # Non-ASCII alphanumeric: the gate is ASCII-only, never clamped.
    "é": _v2_entry(
        "PU0,0;PD1000,0;PD1000,1080;PD0,1080;PD0,0;",
        (0, 0, 1000, 1080),
        [(0, 0), (0, 1080)],
        [(1000, 0), (1000, 1080)],
    ),
    # Derived-style symbol above cap: non-alphanumeric, never clamped.
    "–": _v2_entry(
        "PU0,400;PD1000,400;PD1000,1080;PD0,1080;PD0,400;",
        (0, 400, 1000, 1080),
        [(0, 400), (0, 1080)],
        [(1000, 400), (1000, 1080)],
    ),
}


@pytest.fixture
def cap_lib(tmp_path: Path) -> Path:
    """A synthetic v2 library with overshooting alphanumeric glyphs."""
    return _write_library(tmp_path, _CAP_ENFORCE_CHARACTERS)


class TestCapLineGeometryHelper:
    """``_capline_geometry`` clamps a geometry top-down onto the capline."""

    def test_overshoot_clamped_exactly_to_cap(self) -> None:
        """The top lands on the capline; bottom and X stay exactly put."""
        geometry = _GlyphGeometry(
            block=TextBlock(
                strokes=(
                    Stroke(
                        pen_up=complex(0.0, 0.0),
                        segments=(LineSeg(complex(0.0, 0.0), complex(1.0, 1.05)),),
                    ),
                )
            ),
            bounding_box=(0.0, 0.0, 1.0, 1.05),
            left_envelope=((0.0, 0.0), (0.0, 1.05)),
            right_envelope=((1.0, 0.0), (1.0, 1.05)),
        )
        clamped = _capline_geometry(geometry, 1.0)
        assert clamped.bounding_box == pytest.approx((0.0, 0.0, 1.0, 1.0))
        # The envelope top sample lands on the capline, x untouched.
        assert clamped.left_envelope[-1] == pytest.approx((0.0, 1.0))
        assert clamped.right_envelope[-1] == pytest.approx((1.0, 1.0))
        bounds = clamped.block.bounds()
        assert bounds is not None
        assert bounds[3] == pytest.approx(1.0, abs=1e-9)
        assert bounds[1] == pytest.approx(0.0, abs=1e-9)

    def test_undershoot_returns_the_same_object(self) -> None:
        """A glyph at/below the capline is returned unchanged (identity)."""
        geometry = _GlyphGeometry(
            block=TextBlock(
                strokes=(
                    Stroke(
                        pen_up=complex(0.0, 0.0),
                        segments=(LineSeg(complex(0.0, 0.0), complex(1.0, 1.0)),),
                    ),
                )
            ),
            bounding_box=(0.0, 0.0, 1.0, 1.0),
            left_envelope=(),
            right_envelope=(),
        )
        assert _capline_geometry(geometry, 1.0) is geometry

    def test_degenerate_zero_height_returns_the_same_object(self) -> None:
        """A zero-height glyph has no scale factor: returned unchanged."""
        geometry = _GlyphGeometry(
            block=TextBlock(
                strokes=(
                    Stroke(
                        pen_up=complex(0.0, 1.2),
                        segments=(LineSeg(complex(0.0, 1.2), complex(1.0, 1.2)),),
                    ),
                )
            ),
            bounding_box=(0.0, 1.2, 1.0, 1.2),
            left_envelope=(),
            right_envelope=(),
        )
        assert _capline_geometry(geometry, 1.0) is geometry

    def test_descender_bottom_is_the_anchor(self) -> None:
        """A sub-baseline glyph clamps about its own lowest ink, not y=0."""
        geometry = _GlyphGeometry(
            block=TextBlock(
                strokes=(
                    Stroke(
                        pen_up=complex(0.0, -0.5),
                        segments=(LineSeg(complex(0.0, -0.5), complex(0.5, 1.1)),),
                    ),
                )
            ),
            bounding_box=(0.0, -0.5, 0.5, 1.1),
            left_envelope=((0.0, -0.5), (0.0, 1.1)),
            right_envelope=((0.5, -0.5), (0.5, 1.1)),
        )
        clamped = _capline_geometry(geometry, 1.0)
        assert clamped.bounding_box[1] == pytest.approx(-0.5)
        assert clamped.bounding_box[3] == pytest.approx(1.0)
        bounds = clamped.block.bounds()
        assert bounds is not None
        assert bounds[1] == pytest.approx(-0.5, abs=1e-9)
        assert bounds[3] == pytest.approx(1.0, abs=1e-9)

    def test_arcs_flatten_under_the_clamp(self) -> None:
        """The non-uniform Y scale flattens arcs (the scale_y policy)."""
        geometry = _GlyphGeometry(
            block=TextBlock(
                strokes=(
                    Stroke(
                        pen_up=complex(0.0, 525.0),
                        segments=(ArcSeg(complex(0.0, 525.0), complex(500.0, 525.0), 360.0),),
                    ),
                )
            ),
            bounding_box=(0.0, 0.0, 1000.0, 1050.0),
            left_envelope=(),
            right_envelope=(),
        )
        clamped = _capline_geometry(geometry, 1000.0)
        arcs = sum(
            1
            for stroke in clamped.block.strokes
            for seg in stroke.segments
            if isinstance(seg, ArcSeg)
        )
        lines = sum(
            1
            for stroke in clamped.block.strokes
            for seg in stroke.segments
            if isinstance(seg, LineSeg)
        )
        assert arcs == 0
        assert lines > 0
        bounds = clamped.block.bounds()
        assert bounds is not None
        assert bounds[3] <= 1000.0 + 1e-6


class TestWalkLineCapLineEnforcement:
    """``enforce_cap_line`` clamps alphanumerics first, before all markup."""

    def test_overshooting_cap_clamped_to_capline(self, cap_lib: Path) -> None:
        """The overshooting ``O`` top lands exactly on the capline."""
        plain = _render("O", 1.0, cap_lib)
        enforced = _render("O", 1.0, cap_lib, enforce_cap_line=True)
        assert plain.bounds()[3] == pytest.approx(1.05, abs=1e-9)
        assert enforced.bounds()[3] == pytest.approx(1.0, abs=1e-9)
        # X untouched: the clamp changes the aspect ratio, not the width.
        assert enforced.bounds()[2] == pytest.approx(plain.bounds()[2], abs=1e-9)

    def test_default_off_is_bit_identical(self, cap_lib: Path) -> None:
        """``enforce_cap_line=False`` (the default) matches the plain render."""
        plain = _render("OV", 1.0, cap_lib, cutter_diameter=0.02)
        off = _render("OV", 1.0, cap_lib, cutter_diameter=0.02, enforce_cap_line=False)
        assert off.bounds() == pytest.approx(plain.bounds())

    def test_exact_cap_glyph_never_moves(self, cap_lib: Path) -> None:
        """A glyph whose top sits exactly at the capline is untouched."""
        plain = _render("H", 1.0, cap_lib)
        enforced = _render("H", 1.0, cap_lib, enforce_cap_line=True)
        assert enforced.bounds() == pytest.approx(plain.bounds())

    def test_punctuation_never_clamped(self, cap_lib: Path) -> None:
        """The overshooting ``!`` is outside the alphanumeric gate."""
        plain = _render("!", 1.0, cap_lib)
        enforced = _render("!", 1.0, cap_lib, enforce_cap_line=True)
        assert enforced.bounds() == pytest.approx(plain.bounds())
        assert enforced.bounds()[3] == pytest.approx(1.1, abs=1e-9)

    def test_derived_symbol_never_clamped(self, cap_lib: Path) -> None:
        """A non-alphanumeric symbol above the capline engraves verbatim."""
        plain = _render("–", 1.0, cap_lib)
        enforced = _render("–", 1.0, cap_lib, enforce_cap_line=True)
        assert enforced.bounds() == pytest.approx(plain.bounds())

    def test_non_ascii_alnum_never_clamped(self, cap_lib: Path) -> None:
        """The gate is ASCII-only: ``é`` keeps its overshoot."""
        plain = _render("é", 1.0, cap_lib)
        enforced = _render("é", 1.0, cap_lib, enforce_cap_line=True)
        assert enforced.bounds() == pytest.approx(plain.bounds())

    def test_descender_bottom_stays_put(self, cap_lib: Path) -> None:
        """``g`` keeps its sub-baseline depth; only the top descends."""
        enforced = _render("g", 1.0, cap_lib, enforce_cap_line=True)
        bounds = enforced.bounds()
        assert bounds[1] == pytest.approx(-0.5, abs=1e-9)
        assert bounds[3] == pytest.approx(1.0, abs=1e-9)
        assert bounds[2] == pytest.approx(0.5, abs=1e-9)

    def test_clamp_runs_before_small_caps(self, cap_lib: Path) -> None:
        """SC shrinks the CLAMPED capital: top = sc_scale * cap, not * 1.05."""
        sc = _render(
            "o",
            1.0,
            cap_lib,
            lowercase_scale=0.7,
            lowercase_flags=(True,),
            enforce_cap_line=True,
        )
        assert sc.bounds()[3] == pytest.approx(0.7, abs=1e-9)
        unenforced = _render("o", 1.0, cap_lib, lowercase_scale=0.7, lowercase_flags=(True,))
        assert unenforced.bounds()[3] == pytest.approx(0.735, abs=1e-9)

    def test_clamp_runs_before_bold_scale(self, cap_lib: Path) -> None:
        """The bold baseline scale composes on the clamped silhouette."""
        bold = _render("O", 1.0, cap_lib, glyph_scales=(0.5,), enforce_cap_line=True)
        assert bold.bounds()[3] == pytest.approx(0.5, abs=1e-9)
        unenforced = _render("O", 1.0, cap_lib, glyph_scales=(0.5,))
        assert unenforced.bounds()[3] == pytest.approx(0.525, abs=1e-9)

    def test_clamp_runs_before_italic(self, cap_lib: Path) -> None:
        """The shear keeps the clamped top; the overhang uses the clamped top."""
        italic = _render("O", 1.0, cap_lib, italic_tan=0.5, enforce_cap_line=True)
        bounds = italic.bounds()
        assert bounds[3] == pytest.approx(1.0, abs=1e-9)
        # Right edge = 1.0 (glyph) + 1.0 * 0.5 (overhang from the clamped top).
        assert bounds[2] == pytest.approx(1.5, abs=1e-9)

    def test_sup_shift_composes_on_the_clamped_glyph(self, cap_lib: Path) -> None:
        """The <sup> translate rides on top of the clamped geometry."""
        sup = _render("O", 1.0, cap_lib, baseline_shifts=(0.1,), enforce_cap_line=True)
        assert sup.bounds()[3] == pytest.approx(1.1, abs=1e-9)
        assert sup.bounds()[1] == pytest.approx(0.1, abs=1e-9)

    def test_kerning_advance_is_symmetry_invariant(self, cap_lib: Path) -> None:
        """A pair of identically clamped glyphs kerns exactly like the plain pair.

        The clamp is a Y-only scale: envelope samples move in Y, their X
        (and so the penetration values) stay put. Both glyphs of a pair
        clamp identically, so the paired samples stay Y-aligned and the
        advance is unchanged -- the clamped silhouette IS what kerns (the
        envelopes transform together, see TestCapLineGeometryHelper).
        """
        plain = _render("VV", 1.0, cap_lib, cutter_diameter=0.0, character_spacing=0.0)
        enforced = _render(
            "VV",
            1.0,
            cap_lib,
            cutter_diameter=0.0,
            character_spacing=0.0,
            enforce_cap_line=True,
        )
        assert enforced.bounds()[2] == pytest.approx(plain.bounds()[2], abs=1e-9)

    def test_glyph_groups_stay_exact(self, cap_lib: Path) -> None:
        """The per-glyph partition is a 1:1 cover under the clamp."""
        block, _words, glyphs = render_text_line_plt_font_with_glyphs(
            "OH", 1.0, "Test", library_dir=cap_lib, enforce_cap_line=True
        )
        assert [char for char, _ in glyphs] == ["O", "H"]
        covered = sorted(index for _char, indices in glyphs for index in indices)
        assert covered == list(range(len(block.strokes)))

    def test_words_wrapper_threads_the_flag(self, cap_lib: Path) -> None:
        """``with_words`` clamps exactly like the plain wrapper."""
        block, _groups = render_text_line_plt_font_with_words(
            "O", 1.0, "Test", library_dir=cap_lib, enforce_cap_line=True
        )
        assert block.bounds()[3] == pytest.approx(1.0, abs=1e-9)

    def test_all_glyphs_at_cap_render_bit_identically(self, cap_lib: Path) -> None:
        """A line of exact-cap glyphs is untouched by the enforcement."""
        plain = _render("HH", 1.0, cap_lib, cutter_diameter=0.02)
        enforced = _render("HH", 1.0, cap_lib, cutter_diameter=0.02, enforce_cap_line=True)
        assert enforced.bounds() == pytest.approx(plain.bounds())

    def test_bundled_dino_digits_clamped(self) -> None:
        """The shipped Dino digits overshoot; the clamp lands them on cap."""
        plain = render_text_line_plt_font("0", 0.5, "Dino")
        enforced = render_text_line_plt_font("0", 0.5, "Dino", enforce_cap_line=True)
        assert plain.bounds()[3] > 0.5 + 1e-6
        assert enforced.bounds()[3] == pytest.approx(0.5, abs=1e-5)


class TestKernShifts:
    """The manual-kern cursor deltas (kern_pos_string/kern_neg_string markers)."""

    def test_positive_shift_widens_the_pair(self, synth_lib: Path) -> None:
        """A +0.1 delta on the second glyph widens the line by exactly 0.1."""
        plain = _render("AB", 1.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0)
        kerned = _render(
            "AB", 1.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0,
            kern_shifts=(0.0, 0.1),
        )
        assert kerned.bounds()[2] - plain.bounds()[2] == pytest.approx(0.1, abs=1e-9)

    def test_negative_shift_narrows_the_pair(self, synth_lib: Path) -> None:
        """A -0.25 delta narrows the pair by exactly 0.25 (no clamp)."""
        plain = _render("CA", 1.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0)
        kerned = _render(
            "CA", 1.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0,
            kern_shifts=(0.0, -0.25),
        )
        # C spans [0, 1.0]; A kerns to origin 0.5 (the diagonal penetration),
        # spanning [0.5, 1.5]. The -0.25 delta pulls A to [0.25, 1.25].
        assert plain.bounds()[2] == pytest.approx(1.5, abs=1e-9)
        assert plain.bounds()[2] - kerned.bounds()[2] == pytest.approx(0.25, abs=1e-9)

    def test_shift_carries_forward_and_accumulates(self, synth_lib: Path) -> None:
        """The shifted glyph is the next pair's reference: deltas accumulate."""
        plain = _render("ABC", 1.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0)
        kerned = _render(
            "ABC", 1.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0,
            kern_shifts=(0.0, 0.1, 0.1),
        )
        # B rides +0.1 and C rides B's origin + its own +0.1: total +0.2.
        assert kerned.bounds()[2] - plain.bounds()[2] == pytest.approx(0.2, abs=1e-9)

    def test_pair_kerns_across_the_marker(self, synth_lib: Path) -> None:
        """The envelope pair kerns as if adjacent; the delta rides on top."""
        plain = _render("TR", 1.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0)
        kerned = _render(
            "TR", 1.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0,
            kern_shifts=(0.0, 0.25),
        )
        # The T/R pair advance is unchanged by the marker; the marker adds 0.25.
        assert kerned.bounds()[2] - plain.bounds()[2] == pytest.approx(0.25, abs=1e-9)

    def test_space_entry_shifts_the_next_word(self, synth_lib: Path) -> None:
        """A marker before a space attaches to the next word's first glyph."""
        plain = _render("A A", 1.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0)
        kerned = _render(
            "A A", 1.0, synth_lib, cutter_diameter=0.0, character_spacing=0.0,
            kern_shifts=(0.0, 0.0, 0.1),
        )
        # The third entry is the second A (markers attach to the next
        # non-space character): its origin rides +0.1, widening the line.
        assert kerned.bounds()[2] - plain.bounds()[2] == pytest.approx(0.1, abs=1e-9)

    def test_none_shifts_render_bit_identically(self, synth_lib: Path) -> None:
        """kern_shifts=None keeps the historical geometry exactly."""
        plain = _render("AB", 1.0, synth_lib, cutter_diameter=0.04, character_spacing=0.02)
        explicit_zero = _render(
            "AB", 1.0, synth_lib, cutter_diameter=0.04, character_spacing=0.02,
            kern_shifts=(0.0, 0.0),
        )
        assert explicit_zero.bounds() == pytest.approx(plain.bounds())

    def test_glyph_groups_exclude_the_marker(self, synth_lib: Path) -> None:
        """Markers never engrave: one glyph group per engraved character."""
        _block, _words, glyphs = render_text_line_plt_font_with_glyphs(
            "AB", 1.0, "Test", library_dir=synth_lib, kern_shifts=(0.0, 0.1)
        )
        assert [char for char, _indices in glyphs] == ["A", "B"]

    def test_length_mismatch_raises(self, synth_lib: Path) -> None:
        """A kern_shifts array of the wrong length is a hard ValueError."""
        with pytest.raises(ValueError, match="kern_shifts length"):
            _render("AB", 1.0, synth_lib, kern_shifts=(0.0,))

    def test_wrappers_thread_the_kwarg(self, synth_lib: Path) -> None:
        """All three public wrappers pass kern_shifts through to the walk."""
        kwargs = dict(cutter_diameter=0.0, character_spacing=0.0, kern_shifts=(0.0, 0.1))
        b1 = render_text_line_plt_font("AB", 1.0, "Test", library_dir=synth_lib, **kwargs)
        b2, _ = render_text_line_plt_font_with_words("AB", 1.0, "Test", library_dir=synth_lib, **kwargs)
        b3, _, _ = render_text_line_plt_font_with_glyphs("AB", 1.0, "Test", library_dir=synth_lib, **kwargs)
        assert b2.bounds() == pytest.approx(b1.bounds())
        assert b3.bounds() == pytest.approx(b1.bounds())
