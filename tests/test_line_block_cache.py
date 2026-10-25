"""Pins for the collision-sequence first-pass line-block cache.

The Phase-3 collision-compression sweep re-renders a label up to 17 times,
changing ONLY the label-level ``collision_compress_by_line`` map, so every
step's first-pass glyph walk (``_render_line_block``) is byte-identical.
``render_label_to_plt`` threads one shared ``_LineBlockCache`` through the
whole collision-resolution sequence (initial render, Phase-2 re-render,
every sweep step, the verification render, the diagnostics render).

Pinned here:

- **Bit-identity**: the full ``render_label_to_plt`` output (PLT bytes,
  bounds, and every reporting map) with the cache equals the same sequence
  run with a null cache, for resolving, unresolvable, clean, WORD-mode,
  baseline-spacing, and markup labels.
- **Zero re-walks**: a resolving collision walks each line exactly once
  across the entire sequence.
- **Pre-compression values**: the cache stores the RAW block; a
  compressed-label-warmed cache never leaks compression into a later
  uncompressed render.
- **Key isolation**: chunk mode and glyph-coverage are part of the key.
- **Defensive copy**: a cache hit returns fresh word-group lists.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, List, Optional, Tuple

import numpy as np
import pytest

from plt_optimizer.generate import label_renderer
from plt_optimizer.generate.label_renderer import (
    TextChunkMode,
    _render_positioned_lines,
    _render_text_local_with_bounds,
    render_label_to_plt,
)
from plt_optimizer.generate.resolution import (
    ResolvedHoleSpec,
    ResolvedLabel,
    ResolvedTextLine,
)

SIDE_HOLES = [
    ResolvedHoleSpec(diameter=0.25, location="left"),
    ResolvedHoleSpec(diameter=0.25, location="right"),
]


def _make_line(
    text: str,
    height: float = 0.5,
    max_h_compress: float = 0.0,
    font: Optional[str] = None,
) -> ResolvedTextLine:
    """Build a cutter-compensated ResolvedTextLine (mirrors the collision tests)."""
    cutter_dia = 0.03
    kwargs: dict[str, Any] = {}
    if font is not None:
        kwargs["font"] = font
    return ResolvedTextLine(
        text=text,
        nominal_text_height=height,
        toolpath_text_height=height - cutter_dia,
        cutter_diameter=cutter_dia,
        character_spacing=0.0,
        line_spacing=0.0,
        max_h_compress=max_h_compress,
        **kwargs,
    )


def _label(
    text: str = "WIDE LABEL TEXT",
    holes: Optional[List[ResolvedHoleSpec]] = None,
    max_h_compress: float = 0.0,
    **overrides: Any,
) -> ResolvedLabel:
    """A 3x1in label whose centered text overlaps the given drill holes."""
    if holes is None:
        holes = SIDE_HOLES
    return ResolvedLabel(
        id="cache_label",
        count=1,
        width=3.0,
        height=1.0,
        margin=0.1,
        h_margin=0.1,
        v_margin=0.1,
        hole_margin=0.1875,
        holes=holes,
        content=[_make_line(text, max_h_compress=max_h_compress)],
        hole_text_collision_distance=0.0,
        hole_cutter_diameter=0.0,
        **overrides,
    )


def _null_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every ``_render_positioned_lines`` call to render with NO cache.

    Strips the ``line_block_cache`` kwarg at the shared seam, so
    ``render_label_to_plt`` runs the full collision-resolution sequence with
    a fresh glyph walk per render (the pre-cache behavior).
    """
    real = _render_positioned_lines

    def _no_cache(label: ResolvedLabel, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("line_block_cache", None)
        return real(label, *args, **kwargs)

    monkeypatch.setattr(label_renderer, "_render_positioned_lines", _no_cache)


def _render_snapshot(rendered: Any) -> dict[str, Any]:
    """Every observable of a RenderedLabel (the bit-identity surface)."""
    return {
        "plt_content": rendered.plt_content,
        "bounds": (rendered.x_min, rendered.y_min, rendered.x_max, rendered.y_max),
        "has_collisions": rendered.has_collisions,
        "collision_detected": rendered.collision_detected,
        "compression_by_line": dict(rendered.compression_by_line),
        "line_spacing_by_line": dict(rendered.line_spacing_by_line),
        "descender_adjustments_by_line": dict(rendered.descender_adjustments_by_line),
        "band_center_offset_inches": rendered.band_center_offset_inches,
        "source_label": rendered.source_label,
    }


def _positioned_snapshot(positioned: Any) -> Any:
    """Comparable snapshot of ``_render_positioned_lines`` output."""
    return [
        (
            line_index,
            [np.asarray(poly) for poly in block.to_vpype_polylines()],
            entry,
            word_groups,
            glyph_groups,
        )
        for line_index, block, entry, word_groups, glyph_groups in positioned
    ]


def _assert_positioned_equal(left: Any, right: Any) -> None:
    """Assert two positioned snapshots are identical (entries exact, geometry close)."""
    assert len(left) == len(right)
    for (li, lb, le, lw, lg), (ri, rb, re, rw, rg) in zip(left, right):
        assert li == ri
        assert le == re
        assert lw == rw
        assert lg == rg
        assert len(lb) == len(rb)
        for lp, rp in zip(lb, rb):
            assert np.allclose(lp, rp)


class TestCacheBitIdentity:
    """The cached sequence renders byte-identical to the null-cache sequence."""

    @pytest.mark.parametrize(
        ("kwargs",),
        [
            pytest.param({"max_h_compress": 0.7}, id="phase3-resolve"),
            pytest.param({"max_h_compress": 0.7, "min_hole_margin": 0.0}, id="phase2-then-phase3"),
            pytest.param({"max_h_compress": 0.05}, id="unresolvable"),
            pytest.param({"max_h_compress": 0.7}, id="baseline-default"),
        ],
    )
    def test_collision_sequence_is_bit_identical(
        self, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
    ) -> None:
        """Full render_label_to_plt output matches the no-cache sequence."""
        label = _label(**kwargs)

        cached = render_label_to_plt(label)
        _null_cache(monkeypatch)
        uncached = render_label_to_plt(label)

        assert _render_snapshot(cached) == _render_snapshot(uncached)

    def test_clean_label_is_bit_identical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A label with no collision renders identically with the cache."""
        label = _label(text="HI", holes=[], max_h_compress=0.7)

        cached = render_label_to_plt(label)
        _null_cache(monkeypatch)
        uncached = render_label_to_plt(label)

        assert _render_snapshot(cached) == _render_snapshot(uncached)

    def test_word_mode_label_is_bit_identical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """WORD chunking (sweep renders LINE, full render WORD) is key-isolated."""
        label = _label(max_h_compress=0.7, text_chunk_mode="word")

        cached = render_label_to_plt(label)
        _null_cache(monkeypatch)
        uncached = render_label_to_plt(label)

        assert _render_snapshot(cached) == _render_snapshot(uncached)
        assert cached.has_collisions is False  # the sweep resolved it in both

    def test_markup_label_is_bit_identical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A styled line (hashable runs in the key) renders identically."""
        label = _label(text="WIDE <b>LABEL</b> TEXT", max_h_compress=0.7)

        cached = render_label_to_plt(label)
        _null_cache(monkeypatch)
        uncached = render_label_to_plt(label)

        assert _render_snapshot(cached) == _render_snapshot(uncached)

    def test_multi_line_label_is_bit_identical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Every line of a multi-line label participates in the cache."""
        label = _label(max_h_compress=0.7)
        label = replace(
            label,
            content=[
                _make_line("WIDE LABEL", max_h_compress=0.7),
                _make_line("SECOND LINE", max_h_compress=0.7),
            ],
        )

        cached = render_label_to_plt(label)
        _null_cache(monkeypatch)
        uncached = render_label_to_plt(label)

        assert _render_snapshot(cached) == _render_snapshot(uncached)


class TestCacheEffectiveness:
    """The sweep walks each line's glyphs exactly once per sequence."""

    def test_resolving_sweep_walks_each_line_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Initial render + sweep + verification share one glyph walk per line."""
        real = label_renderer._render_line_block
        calls: List[Tuple[str, TextChunkMode]] = []

        def _counting(line: ResolvedTextLine, mode: TextChunkMode, **kw: Any) -> Any:
            calls.append((line.text, mode))
            return real(line, mode, **kw)

        monkeypatch.setattr(label_renderer, "_render_line_block", _counting)
        label = _label(max_h_compress=0.7, min_hole_margin=0.0)

        rendered = render_label_to_plt(label)
        assert rendered.has_collisions is False
        # The sweep resolved the collision, so the diagnostics render never ran.
        # Every render after the first is a cache hit: exactly one walk per line.
        assert calls == [(label.content[0].text, TextChunkMode.LINE)]

    def test_sweep_cache_hit_matches_fresh_render(self) -> None:
        """A cache-warmed compressed render equals the fresh render exactly."""
        label = _label(max_h_compress=0.7)
        compressed = replace(label, collision_compress_by_line={0: 0.6})

        cache: Any = {}
        _render_text_local_with_bounds(label, line_block_cache=cache)  # warm
        warmed_lc, warmed_entries = _render_text_local_with_bounds(
            compressed, line_block_cache=cache
        )
        fresh_lc, fresh_entries = _render_text_local_with_bounds(compressed)

        assert warmed_entries == fresh_entries
        warmed_polys = [np.asarray(p) for p in warmed_lc]
        fresh_polys = [np.asarray(p) for p in fresh_lc]
        assert len(warmed_polys) == len(fresh_polys)
        for wp, fp in zip(warmed_polys, fresh_polys):
            assert np.allclose(wp, fp)


class TestCacheKeyContract:
    """The cache stores PRE-compression renders and isolates by mode/coverage."""

    def test_compressed_warm_cache_never_leaks_into_uncompressed_render(self) -> None:
        """Cached blocks are raw: a compressed warm-up cannot shrink a later render.

        ``"WIDE"`` (1.5in) fits the 2.8in inner area, so the uncompressed
        label renders at natural width (scale 1.0). If the cache stored the
        collision-compressed block, the warmed render would come out half as
        wide with a 0.5 effective scale.
        """
        label = _label(text="WIDE", max_h_compress=0.7)
        compressed = replace(label, collision_compress_by_line={0: 0.5})

        cache: Any = {}
        _render_text_local_with_bounds(compressed, line_block_cache=cache)
        _warmed, warmed_entries = _render_text_local_with_bounds(label, line_block_cache=cache)
        _fresh, fresh_entries = _render_text_local_with_bounds(label)

        assert warmed_entries == fresh_entries
        assert warmed_entries[0].compression_scale == pytest.approx(1.0)
        warmed_width = warmed_entries[0].bounds[2] - warmed_entries[0].bounds[0]
        fresh_width = fresh_entries[0].bounds[2] - fresh_entries[0].bounds[0]
        assert warmed_width == pytest.approx(fresh_width)

    def test_chunk_mode_is_part_of_the_key(self) -> None:
        """A LINE-warmed cache cannot serve a WORD render's word groups."""
        label = _label(text="TWO WORDS")

        cache: Any = {}
        _render_positioned_lines(label, TextChunkMode.LINE, line_block_cache=cache)
        positioned = _render_positioned_lines(label, TextChunkMode.WORD, line_block_cache=cache)

        # The WORD render must have produced word groups (a LINE cache hit
        # would have carried None).
        assert positioned[0][3] is not None
        assert len(positioned[0][3]) == 2

    def test_glyph_coverage_is_part_of_the_key(self) -> None:
        """check_glyph_coverage is part of the key (distinct entries)."""
        label = _label(text="AB")

        cache: Any = {}
        _render_positioned_lines(label, TextChunkMode.LINE, line_block_cache=cache)
        _render_positioned_lines(
            label, TextChunkMode.LINE, check_glyph_coverage=False, line_block_cache=cache
        )

        keys = {key[1:] for key in cache}
        assert keys == {(TextChunkMode.LINE, True), (TextChunkMode.LINE, False)}

    def test_cache_hit_returns_fresh_word_group_lists(self) -> None:
        """Word-group lists are copied on a hit: mutating one render cannot poison the cache."""
        label = _label(text="TWO WORDS")

        cache: Any = {}
        first = _render_positioned_lines(label, TextChunkMode.WORD, line_block_cache=cache)
        first[0][3][0][1].append(999)  # poison the first render's list

        second = _render_positioned_lines(label, TextChunkMode.WORD, line_block_cache=cache)
        assert 999 not in second[0][3][0][1]
