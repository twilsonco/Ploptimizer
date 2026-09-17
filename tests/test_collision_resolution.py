"""Unit tests for text-hole collision resolution (Phases 2 and 3).

Phase 2 reduces ``hole_margin`` toward ``min_hole_margin``; Phase 3
compresses text horizontally within the ``max_h_compress`` budget. These
tests drive ``render_label_to_plt`` end-to-end and assert on the adjusted
``source_label`` returned inside each ``RenderedLabel``.

Fixture geometry (verified against the bundled ReliefSingleLine font):
a 3x1in label with 0.1in margin, 0.5in text (0.47in toolpath), and 0.25in
holes at the default 0.1875in hole margin. Vertically centered text spans
y ~= [0.265, 0.735]:

- A ``bottom`` hole's circle top sits at ``hole_margin + 0.25``, so the
  collision clears once ``hole_margin`` drops below ~0.015 -- fixable by
  Phase 2 but NOT by horizontal compression.
- ``left``/``right`` holes are overlapped by any full-width line; centered
  text clears them only by narrowing -- fixable by Phase 3 but NOT by
  margin reduction (the circles stay inside the text box at any margin).
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from plt_optimizer.generate.label_renderer import (
    LabelRenderError,
    _detect_text_hole_collisions,
    _render_text_local_with_bounds,
    _resolve_collision_via_compression,
    _resolve_collision_via_margin_adjustment,
    log_text_hole_collisions,
    render_label_to_plt,
)
from plt_optimizer.generate.resolution import (
    ResolvedHoleSpec,
    ResolvedLabel,
    ResolvedTextLine,
)

LOGGER_NAME = "plt_optimizer.generate.label_renderer"

BOTTOM_HOLE = [ResolvedHoleSpec(diameter=0.25, location="bottom")]
SIDE_HOLES = [
    ResolvedHoleSpec(diameter=0.25, location="left"),
    ResolvedHoleSpec(diameter=0.25, location="right"),
]


def _make_line(text: str, height: float = 0.5, max_h_compress: float = 0.0) -> ResolvedTextLine:
    """Build a cutter-compensated ResolvedTextLine."""
    cutter_dia = 0.03
    return ResolvedTextLine(
        text=text,
        nominal_text_height=height,
        toolpath_text_height=height - cutter_dia,
        cutter_diameter=cutter_dia,
        character_spacing=0.0,
        line_spacing=0.0,
        max_h_compress=max_h_compress,
    )


def _label(
    text: str = "WIDE LABEL TEXT",
    holes: list[ResolvedHoleSpec] | None = None,
    width: float = 3.0,
    height: float = 1.0,
    margin: float = 0.1,
    hole_margin: float = 0.1875,
    min_hole_margin: float | None = None,
    max_h_compress: float = 0.0,
) -> ResolvedLabel:
    """Build a label whose centered text overlaps the given drill holes."""
    if holes is None:
        holes = SIDE_HOLES
    return ResolvedLabel(
        id="resolve_label",
        count=1,
        width=width,
        height=height,
        margin=margin,
        hole_margin=hole_margin,
        holes=holes,
        content=[_make_line(text, max_h_compress=max_h_compress)],
        min_hole_margin=min_hole_margin,
    )


class TestMarginAdjustment:
    """Phase 2: hole-margin reduction toward min_hole_margin."""

    def test_margin_reduction_resolves_collision(self) -> None:
        """A bottom hole clearable at a smaller margin resolves via Phase 2."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, min_hole_margin=0.0)
        rendered = render_label_to_plt(label)

        assert rendered.has_collisions is False
        adjusted = rendered.source_label
        assert adjusted is not label
        assert 0.0 <= adjusted.hole_margin < label.hole_margin
        # The emitted PLT must come from the adjusted label: re-detecting
        # collisions on the adjusted geometry finds none.
        _lc, entries = _render_text_local_with_bounds(adjusted)
        assert _detect_text_hole_collisions(adjusted, entries) == []

    def test_margin_adjustment_respects_min_hole_margin_floor(self) -> None:
        """The reduced margin never drops below the configured floor."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, min_hole_margin=0.01)
        _lc, entries = _render_text_local_with_bounds(label)
        resolved = _resolve_collision_via_margin_adjustment(label, entries)

        assert resolved is not None
        assert resolved.hole_margin >= 0.01 - 1e-9
        assert resolved.hole_margin < label.hole_margin

    def test_floor_too_high_blocks_resolution(self) -> None:
        """A floor above the clearing margin leaves the collision in place."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, min_hole_margin=0.16)
        _lc, entries = _render_text_local_with_bounds(label)
        assert _resolve_collision_via_margin_adjustment(label, entries) is None

        with pytest.raises(LabelRenderError):
            render_label_to_plt(label)

    def test_no_floor_configured_skips_phase2(self) -> None:
        """Without min_hole_margin, margin adjustment is not attempted."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, min_hole_margin=None)
        _lc, entries = _render_text_local_with_bounds(label)
        assert _resolve_collision_via_margin_adjustment(label, entries) is None

    def test_no_budget_when_margin_at_floor(self) -> None:
        """hole_margin already at the floor leaves nothing to shrink."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, hole_margin=0.05, min_hole_margin=0.05)
        _lc, entries = _render_text_local_with_bounds(label)
        assert _resolve_collision_via_margin_adjustment(label, entries) is None

    def test_adjustment_logs_before_and_after_margins(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A successful adjustment logs INFO with both margin values."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, min_hole_margin=0.0)
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            rendered = render_label_to_plt(label)

        messages = [r.message for r in caplog.records if "adjusted hole_margin" in r.message]
        assert messages, "No INFO logged for margin adjustment"
        assert "from 0.1875in" in messages[0]
        assert f"to {rendered.source_label.hole_margin:.4f}in" in messages[0]

    def test_side_holes_not_fixable_by_margin(self) -> None:
        """Full-width centered text over side holes ignores margin reduction."""
        label = _label(min_hole_margin=0.0)
        _lc, entries = _render_text_local_with_bounds(label)
        assert _resolve_collision_via_margin_adjustment(label, entries) is None

    def test_margin_already_at_floor_raises(self) -> None:
        """With hole_margin pinned at the floor, Phase 2 has no budget to shrink."""
        label = _label(text="HELLO", holes=BOTTOM_HOLE, hole_margin=0.05, min_hole_margin=0.05)
        with pytest.raises(LabelRenderError):
            render_label_to_plt(label)

    def test_log_text_hole_collisions_no_holes_returns_empty(self) -> None:
        """The observational helper short-circuits for hole-free labels."""
        label = _label(text="HELLO", holes=[])
        assert log_text_hole_collisions(label) == []


class TestCompressionFallback:
    """Phase 3: horizontal compression when margins cannot help."""

    def test_compression_resolves_side_hole_collision(self) -> None:
        """Ample max_h_compress narrows centered text clear of side holes."""
        label = _label(max_h_compress=0.7)
        rendered = render_label_to_plt(label)

        assert rendered.has_collisions is False
        adjusted = rendered.source_label
        assert adjusted.collision_compress < 1.0
        # Never compress below the configured budget floor (1 - 0.7).
        assert adjusted.collision_compress >= 1.0 - 0.7 - 1e-9

    def test_compression_respects_max_h_compress_limit(self) -> None:
        """A budget smaller than required cannot resolve the collision.

        Without ``min_hole_margin`` (the dedicated collision-avoidance
        opt-in) the render degrades to log-only instead of aborting, since
        ``max_h_compress`` is a shared margin-fitting knob.
        """
        label = _label(max_h_compress=0.05)
        rendered = render_label_to_plt(label)
        assert rendered.has_collisions is True
        # The unmodified render is returned: no partial compression is kept.
        assert rendered.source_label is label

    def test_compression_only_failure_never_raises(self) -> None:
        """Compression failure without min_hole_margin degrades to a warning."""
        label = _label(max_h_compress=0.0)
        # No exception even though the collision is unfixable by compression.
        render_label_to_plt(label)

    def test_compression_skipped_when_budget_disabled(self) -> None:
        """max_h_compress=0.0 on every line skips Phase 3 entirely."""
        label = _label(max_h_compress=0.0)
        rendered = render_label_to_plt(label)
        # Avoidance disabled: logged-only, unmodified render.
        assert rendered.source_label is label
        assert rendered.has_collisions is True

    def test_compression_stacks_on_top_of_margin_floor(self) -> None:
        """With both phases enabled, compression applies on top of the floor."""
        label = _label(
            holes=[
                ResolvedHoleSpec(diameter=0.25, location="bottom"),
                ResolvedHoleSpec(diameter=0.25, location="left"),
            ],
            min_hole_margin=0.0,
            max_h_compress=0.7,
        )
        rendered = render_label_to_plt(label)
        adjusted = rendered.source_label
        # The bottom hole clears only by shrinking the margin to the floor;
        # the left hole (text spans the full inner width at scale 1.0)
        # additionally requires compression. Both must have been applied.
        assert adjusted.hole_margin < 0.1875
        assert adjusted.collision_compress < 1.0
        assert rendered.has_collisions is False

    def test_compression_helper_returns_none_at_floor(self) -> None:
        """A label already at the compression floor cannot compress more."""
        label = _label(max_h_compress=1.0)
        at_floor = replace(label, collision_compress=0.0)
        assert _resolve_collision_via_compression(at_floor, 1.0) is None

    def test_unresolvable_error_lists_diagnostic_information(self) -> None:
        """The raised error names margins, compression, and recommendations."""
        label = _label(max_h_compress=0.05, min_hole_margin=0.15)
        with pytest.raises(LabelRenderError) as exc_info:
            render_label_to_plt(label)

        message = str(exc_info.value)
        assert "resolve_label" in message
        assert "Hole margin" in message
        assert "min: 0.15" in message
        assert "max_h_compress=0.05" in message
        assert "penetration" in message
        assert "Recommendations" in message
