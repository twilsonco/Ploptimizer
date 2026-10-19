"""Pins for the descender-relief memoization (the sweep-cache perf cycle).

The relief re-measures the SAME line ink over and over: every fixpoint pass
re-indexes the unchanged lines with a fresh :class:`InkGrid`, and every
collision-sweep step re-renders a label whose non-compressed lines are
byte-identical (the sweep scales X only, on colliding lines), rebuilding
every line's per-glyph ink boxes. All three leaves are pure functions of
hashable inputs and their results are read-only, so all are memoized:

- :func:`plt_optimizer.generate.geometry.ink_grid_for` — LRU-memoized grid
  construction (``InkGrid`` is read-only after ``__init__``);
- ``label_renderer._glyph_boxes`` — LRU-memoized glyph-box tuple, returned
  as a fresh list per call;
- ``label_renderer._shifted_boxes`` — LRU-memoized stacked-frame shift (the
  relief re-shifts unchanged lines onto unchanged baselines every fixpoint
  pass), returned as a fresh list per call.

Pinned here:

- **Identity hits**: equal hashable keys return the SAME object; a fresh
  list is returned per ``_glyph_boxes`` call (callers own their result).
- **Key isolation**: cell size / block content / partition / text are part
  of the key — a changed input never hits a stale entry.
- **Unhashable fallback**: list-built ink measures correctly, unmemoized.
- **Randomized brute-force equivalence**: memoized results equal the
  cache-cleared recomputation (grid gaps and glyph boxes).
- **LRU bounds**: exceeding the cache size evicts (memory stays bounded).
- **Pipeline bit-identity**: a cache-warm ``render_label_to_plt`` on a
  label whose relief fires equals the cache-cold render on every observable.
"""

from __future__ import annotations

import random
from typing import Any, List, Tuple

import pytest

from plt_optimizer.generate.descender_adjust import GlyphBox
from plt_optimizer.generate.geometry import (
    _INK_GRID_CACHE_SIZE,
    InkGrid,
    _cached_ink_grid,
    ink_gap,
    ink_grid_for,
)
from plt_optimizer.generate.label_renderer import (
    _GLYPH_BOX_CACHE_SIZE,
    _SHIFTED_BOX_CACHE_SIZE,
    _cached_glyph_boxes,
    _cached_shifted_boxes,
    _glyph_boxes,
    _shifted_boxes,
    render_label_to_plt,
)
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine
from plt_optimizer.generate.text_geometry import LineSeg, Stroke, TextBlock


def _clear_memos() -> None:
    """Empty the memo caches (the cold-start state)."""
    _cached_ink_grid.cache_clear()
    _cached_glyph_boxes.cache_clear()
    _cached_shifted_boxes.cache_clear()


def _random_ink(seed: int, region: float, polylines: int, vertices: int) -> Any:
    """Build deterministic pseudo-random ink inside ``[0, region]^2``."""
    rng = random.Random(seed)
    return tuple(
        tuple(
            (rng.uniform(0.0, region), rng.uniform(0.0, region))
            for _ in range(rng.randint(1, vertices))
        )
        for _ in range(polylines)
    )


def _random_block(seed: int) -> Tuple[TextBlock, Tuple[Tuple[int, ...], ...], str]:
    """Build a random 4-glyph line block (2 strokes per glyph) + partition."""
    rng = random.Random(seed)
    strokes: List[Stroke] = []
    groups: List[Tuple[int, ...]] = []
    for glyph in range(4):
        indices: List[int] = []
        for _stroke in range(2):
            x = glyph * 1.0 + rng.uniform(0.0, 0.6)
            y = rng.uniform(-0.3, 0.7)
            start = complex(x, y)
            end = complex(x + rng.uniform(-0.2, 0.2), y - rng.uniform(0.0, 0.4))
            strokes.append(Stroke(pen_up=start, segments=(LineSeg(start, end),)))
            indices.append(len(strokes) - 1)
        groups.append(tuple(indices))
    text = "abcd"  # 4 non-space chars, one per group
    return TextBlock(strokes=tuple(strokes)), tuple(groups), text


class TestInkGridMemoization:
    """``ink_grid_for`` shares grids by (ink, cell) without changing any gap."""

    def setup_method(self) -> None:
        _clear_memos()

    def test_equal_hashable_ink_returns_the_same_grid(self) -> None:
        """A repeat (ink, cell) key hits the memo (object identity)."""
        ink = _random_ink(1, region=2.0, polylines=3, vertices=4)
        first = ink_grid_for(ink, 0.1)
        twin = tuple(tuple(point for point in line) for line in ink)  # equal, not identical
        assert ink_grid_for(twin, 0.1) is first

    def test_cell_size_is_part_of_the_key(self) -> None:
        """The same ink at a different cell is a different grid."""
        ink = _random_ink(2, region=2.0, polylines=2, vertices=3)
        assert ink_grid_for(ink, 0.1) is not ink_grid_for(ink, 0.25)

    def test_queries_are_repeatable_on_a_shared_grid(self) -> None:
        """``min_gap`` mutates nothing: a shared grid re-answers identically."""
        lower = _random_ink(3, region=2.0, polylines=3, vertices=4)
        upper = _random_ink(4, region=2.0, polylines=2, vertices=3)
        grid = ink_grid_for(lower, 0.5)
        assert grid.min_gap(upper) == grid.min_gap(upper)

    def test_unhashable_ink_falls_back_unmemoized(self) -> None:
        """List-built ink (tests/hand-built) measures correctly, unbuilt-shared."""
        ink_lists = [[(0.0, 0.0), (1.0, 0.0)]]
        grid = ink_grid_for(ink_lists, 0.5)  # type: ignore[arg-type]
        assert grid.min_gap((((0.0, 0.25), (1.0, 0.25)),)) == pytest.approx(0.25)
        assert ink_grid_for(ink_lists, 0.5) is not grid  # list key: never shared

    def test_randomized_equivalence_with_the_plain_constructor(self) -> None:
        """The memoized grid measures exactly what ``InkGrid`` measures."""
        for seed in range(30):
            lower = _random_ink(seed, region=3.0, polylines=3, vertices=5)
            upper = _random_ink(seed + 500, region=3.0, polylines=2, vertices=5)
            for cell in (0.05, 0.25, 1.0):
                memo = ink_grid_for(lower, cell).min_gap(upper)
                plain = InkGrid(lower, cell).min_gap(upper)
                assert memo == plain, (seed, cell)

    def test_memoized_grid_stays_exact_below_the_cell(self) -> None:
        """Warm hits keep the exactness contract (== ink_gap below the cell)."""
        for seed in range(20):
            lower = _random_ink(seed, region=2.0, polylines=3, vertices=4)
            upper = _random_ink(seed + 900, region=2.0, polylines=2, vertices=4)
            cell = 0.5
            true_gap = ink_gap(upper, lower)
            ink_grid_for(lower, cell)  # warm the memo
            measured = ink_grid_for(lower, cell).min_gap(upper)
            if true_gap < cell:
                assert measured == pytest.approx(true_gap, abs=1e-9), seed

    def test_cache_is_bounded(self) -> None:
        """One key past the LRU bound evicts the oldest (memory stays bounded)."""
        keys = [
            _random_ink(100 + i, region=1.0, polylines=1, vertices=2)
            for i in range(_INK_GRID_CACHE_SIZE + 1)
        ]
        first = ink_grid_for(keys[0], 0.1)
        for key in keys[1:]:
            ink_grid_for(key, 0.1)
        assert ink_grid_for(keys[0], 0.1) is not first  # evicted, rebuilt
        twin = tuple(tuple(point for point in line) for line in keys[0])
        rebuilt = ink_grid_for(twin, 0.1)
        assert rebuilt.cell_size == first.cell_size  # equal by construction


class TestGlyphBoxMemoization:
    """``_glyph_boxes`` shares per-line glyph boxes without changing geometry."""

    def setup_method(self) -> None:
        _clear_memos()

    def test_repeat_call_returns_equal_boxes_in_a_fresh_list(self) -> None:
        """A (block, groups, text) hit shares GlyphBox objects, not the list."""
        block, groups, text = _random_block(1)
        first = _glyph_boxes(block, groups, text)
        second = _glyph_boxes(block, groups, text)
        assert first is not second  # callers own their list
        assert [box.glyph_index for box in first] == [box.glyph_index for box in second]
        for a, b in zip(first, second):
            assert a is b  # memo hit: same immutable GlyphBox

    def test_mutating_the_returned_list_never_poisons_the_memo(self) -> None:
        """Callers can pop/append their list; the cached tuple is untouched."""
        block, groups, text = _random_block(2)
        expected = len(_glyph_boxes(block, groups, text))
        scratch = _glyph_boxes(block, groups, text)
        scratch.clear()
        assert len(_glyph_boxes(block, groups, text)) == expected

    def test_different_block_content_is_a_different_key(self) -> None:
        """A translated block (equal partition/text) never hits the old entry."""
        block, groups, text = _random_block(3)
        moved = block.translate(5.0, 0.0)
        original = _glyph_boxes(block, groups, text)
        shifted = _glyph_boxes(moved, groups, text)
        assert original[0].bounds[0] != shifted[0].bounds[0]
        assert original[0].ink[0][0][0] != shifted[0].ink[0][0][0]

    def test_partition_and_text_are_part_of_the_key(self) -> None:
        """Same block, other partition/text: no stale hit."""
        block, groups, text = _random_block(4)
        base = _glyph_boxes(block, groups, text)
        # A shorter partition is a different key (and a shorter text matches it).
        halved = tuple(groups[:2])
        assert _glyph_boxes(block, halved, "ab") != base
        # Equal-length partition, different text: fresh entry, same geometry,
        # new character names (the memo never carries stale chars over).
        renamed = _glyph_boxes(block, groups, "wxyz")
        assert [box.char for box in renamed] == ["w", "x", "y", "z"]
        assert [box.bounds for box in renamed] == [box.bounds for box in base]

    def test_empty_partition_is_memoized_too(self) -> None:
        """The no-partition no-op (relief disabled by geometry) returns []."""
        block, _groups, text = _random_block(5)
        assert _glyph_boxes(block, (), text) == []
        assert _glyph_boxes(block, (), text) == []

    def test_randomized_equivalence_with_a_cache_cleared_recompute(self) -> None:
        """Warm results equal cold results on random blocks."""
        for seed in range(20):
            block, groups, text = _random_block(seed)
            warm_a = _glyph_boxes(block, groups, text)
            _glyph_boxes(block, groups, text)  # warm
            warm_b = _glyph_boxes(block, groups, text)
            _clear_memos()
            cold = _glyph_boxes(block, groups, text)
            assert warm_a == warm_b == cold, seed
            for a, b in zip(warm_a, cold):
                assert a.ink == b.ink
                assert a.sub_baseline_ink == b.sub_baseline_ink
                assert a.descender == b.descender

    def test_descender_floor_is_memo_stable(self) -> None:
        """Shallow sub-baseline ink zeroes the descender identically warm/cold."""
        start, end = complex(0.0, 0.05), complex(0.5, -0.0001)  # below the emission floor
        block = TextBlock(strokes=(Stroke(pen_up=start, segments=(LineSeg(start, end),)),))
        groups = ((0,),)
        boxes = _glyph_boxes(block, groups, "p")
        _clear_memos()
        cold = _glyph_boxes(block, groups, "p")
        assert boxes == cold
        assert boxes[0].descender == 0.0

    def test_cache_is_bounded(self) -> None:
        """One key past the LRU bound evicts the oldest (memory stays bounded)."""
        blocks = [_random_block(200 + i) for i in range(_GLYPH_BOX_CACHE_SIZE + 1)]
        first = _glyph_boxes(*blocks[0])
        for args in blocks[1:]:
            _glyph_boxes(*args)
        second = _glyph_boxes(*blocks[0])
        assert first[0] is not second[0]  # evicted, rebuilt
        assert first == second  # ...equal by construction

    def test_list_partition_is_accepted(self) -> None:
        """A list-built partition works (coerced into the hashable key)."""
        block, groups, text = _random_block(6)
        as_lists = [list(group) for group in groups]
        assert _glyph_boxes(block, as_lists, text) == _glyph_boxes(block, groups, text)  # type: ignore[arg-type]


def _plain_shift(box: GlyphBox, baseline_y: float) -> GlyphBox:
    """Brute-force reference shift of one glyph box (no memo, no helpers)."""
    return GlyphBox(
        glyph_index=box.glyph_index,
        char=box.char,
        bounds=(
            box.bounds[0],
            box.bounds[1] + baseline_y,
            box.bounds[2],
            box.bounds[3] + baseline_y,
        ),
        descender=box.descender,
        ink=tuple(tuple((x, y + baseline_y) for x, y in line) for line in box.ink),
        sub_baseline_ink=tuple(
            tuple((x, y + baseline_y) for x, y in line) for line in box.sub_baseline_ink
        ),
    )


class TestShiftedBoxesMemoization:
    """``_shifted_boxes`` shares stacked-frame boxes without changing geometry."""

    def setup_method(self) -> None:
        _clear_memos()

    def _boxes(self, seed: int) -> List[GlyphBox]:
        """Local-frame glyph boxes of a random block."""
        block, groups, text = _random_block(seed)
        return _glyph_boxes(block, groups, text)

    def test_repeat_call_returns_equal_boxes_in_a_fresh_list(self) -> None:
        """A (boxes, baseline_y) hit shares GlyphBox objects, not the list."""
        boxes = self._boxes(11)
        first = _shifted_boxes(boxes, 0.75)
        second = _shifted_boxes(boxes, 0.75)
        assert first is not second  # callers own their list
        for a, b in zip(first, second):
            assert a is b  # memo hit: same immutable GlyphBox

    def test_baseline_is_part_of_the_key(self) -> None:
        """The same boxes on a different baseline never hit the old entry."""
        boxes = self._boxes(12)
        low = _shifted_boxes(boxes, 0.5)
        high = _shifted_boxes(boxes, 1.25)
        assert low[0].bounds[1] != high[0].bounds[1]
        assert low[0].ink[0][0][1] != high[0].ink[0][0][1]

    def test_box_content_is_part_of_the_key(self) -> None:
        """A changed local box (same baseline) is a fresh entry."""
        boxes = self._boxes(13)
        base = _shifted_boxes(boxes, 0.5)
        moved = [
            GlyphBox(
                glyph_index=box.glyph_index,
                char=box.char,
                bounds=(box.bounds[0] + 3.0, *box.bounds[1:]),
                descender=box.descender,
                ink=box.ink,
                sub_baseline_ink=box.sub_baseline_ink,
            )
            for box in boxes
        ]
        shifted = _shifted_boxes(moved, 0.5)
        assert base[0].bounds[0] != shifted[0].bounds[0]

    def test_zero_baseline_bypasses_the_memo(self) -> None:
        """The identity shift stays the cheap uncached copy it always was."""
        boxes = self._boxes(14)
        result = _shifted_boxes(boxes, 0.0)
        assert result == list(boxes)
        assert result is not boxes
        assert _cached_shifted_boxes.cache_info().misses == 0

    def test_mutating_the_returned_list_never_poisons_the_memo(self) -> None:
        """Callers can pop/append their list; the cached tuple is untouched."""
        boxes = self._boxes(15)
        expected = len(_shifted_boxes(boxes, 0.5))
        scratch = _shifted_boxes(boxes, 0.5)
        scratch.clear()
        assert len(_shifted_boxes(boxes, 0.5)) == expected

    def test_randomized_equivalence_with_a_brute_force_shift(self) -> None:
        """Warm and cold results equal the plain per-box shift on random ink."""
        for seed in range(20):
            boxes = self._boxes(seed)
            for baseline in (-0.7, 0.001, 0.5, 2.25):
                warm_a = _shifted_boxes(boxes, baseline)
                warm_b = _shifted_boxes(boxes, baseline)  # warm
                _cached_shifted_boxes.cache_clear()
                cold = _shifted_boxes(boxes, baseline)
                expected = [_plain_shift(box, baseline) for box in boxes]
                assert warm_a == warm_b == cold == expected, (seed, baseline)
                for box, shifted in zip(boxes, cold):
                    assert shifted.ink == expected[box.glyph_index].ink
                    assert shifted.sub_baseline_ink == expected[box.glyph_index].sub_baseline_ink

    def test_cache_is_bounded(self) -> None:
        """One key past the LRU bound evicts the oldest (memory stays bounded)."""
        boxes = self._boxes(16)
        baselines = [0.001 * (i + 1) for i in range(_SHIFTED_BOX_CACHE_SIZE + 1)]
        first = _shifted_boxes(boxes, baselines[0])
        for baseline in baselines[1:]:
            _shifted_boxes(boxes, baseline)
        second = _shifted_boxes(boxes, baselines[0])
        assert first[0] is not second[0]  # evicted, rebuilt
        assert first == second  # ...equal by construction

    def test_list_partition_is_accepted(self) -> None:
        """A list-built box sequence works (coerced into the hashable key)."""
        boxes = self._boxes(17)
        assert _shifted_boxes(boxes, 0.5) == _shifted_boxes(tuple(boxes), 0.5)


def _make_line(
    text: str,
    height: float = 0.5,
    line_spacing: float = 0.05,
    max_h_compress: float = 0.0,
) -> ResolvedTextLine:
    """Build a cutter-compensated ResolvedTextLine for relief rendering."""
    cutter_dia = 0.03
    return ResolvedTextLine(
        text=text,
        nominal_text_height=height,
        toolpath_text_height=height - cutter_dia,
        cutter_diameter=cutter_dia,
        character_spacing=0.0,
        line_spacing=line_spacing,
        max_h_compress=max_h_compress,
    )


def _relief_label() -> ResolvedLabel:
    """A two-line label whose descenders force the relief to fire."""
    return ResolvedLabel(
        id="memo_label",
        count=1,
        width=4.0,
        height=1.6,
        margin=0.1,
        h_margin=0.1,
        v_margin=0.1,
        holes=[],
        content=[_make_line("Typography"), _make_line("quipping")],
        descending_character_clearance=1.0,
    )


def _render_snapshot(rendered: Any) -> dict[str, Any]:
    """Every observable of a RenderedLabel (the bit-identity surface)."""
    return {
        "plt_content": rendered.plt_content,
        "bounds": (rendered.x_min, rendered.y_min, rendered.x_max, rendered.y_max),
        "has_collisions": rendered.has_collisions,
        "collision_detected": rendered.collision_detected,
        "compression_by_line": dict(rendered.compression_by_line),
        "line_spacing_by_line": dict(rendered.line_spacing_by_line),
        "descender_adjustments_by_line": {
            line: list(fixes) for line, fixes in rendered.descender_adjustments_by_line.items()
        },
        "band_center_offset_inches": rendered.band_center_offset_inches,
        "source_label": rendered.source_label,
    }


class TestReliefRenderBitIdentity:
    """A memo-warm render equals the cold render on a relief-firing label."""

    def test_warm_render_is_bit_identical_to_cold(self) -> None:
        """The relief fires; the second (all-hits) render changes nothing."""
        _clear_memos()
        label = _relief_label()
        cold = render_label_to_plt(label)
        assert cold.descender_adjustments_by_line  # the relief really fired
        warm = render_label_to_plt(label)
        snapshot_cold = _render_snapshot(cold)
        snapshot_warm = _render_snapshot(warm)
        assert snapshot_cold.keys() == snapshot_warm.keys()
        for key in snapshot_cold:
            assert snapshot_cold[key] == snapshot_warm[key], key

    def test_memos_survive_a_full_collision_sequence(self) -> None:
        """Sweep + relief share the memos; the resolved render stays identical."""
        from plt_optimizer.generate.resolution import ResolvedHoleSpec

        _clear_memos()
        label = ResolvedLabel(
            id="memo_sweep",
            count=1,
            width=4.0,
            height=1.6,
            margin=0.1,
            h_margin=0.1,
            v_margin=0.1,
            hole_margin=0.1875,
            holes=[
                ResolvedHoleSpec(diameter=0.25, location="left"),
                ResolvedHoleSpec(diameter=0.25, location="right"),
            ],
            content=[_make_line("Typography gy", line_spacing=0.05, max_h_compress=0.3)],
            hole_text_collision_distance=0.0,
            hole_cutter_diameter=0.0,
        )
        cold = render_label_to_plt(label)
        assert cold.collision_detected
        warm = render_label_to_plt(label)
        assert cold.plt_content == warm.plt_content
        assert cold.source_label == warm.source_label

    def test_grid_memo_shared_across_relief_passes(self) -> None:
        """Repeated relief calls reuse grids (the sweep-cache win, pinned)."""
        _clear_memos()
        label = _relief_label()
        render_label_to_plt(label)
        stats_after_cold = _cached_ink_grid.cache_info()
        render_label_to_plt(label)
        stats_after_warm = _cached_ink_grid.cache_info()
        assert stats_after_warm.hits > stats_after_cold.hits + stats_after_cold.misses
        assert stats_after_warm.misses == stats_after_cold.misses  # zero new builds
