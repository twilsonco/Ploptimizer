"""Unit tests for the compression-driven cutter reduction pre-pass.

``plt_optimizer.generate.cutter_downsize`` renders each label once, reads the
effective per-line horizontal scales, and swaps the *automatic* cutter of a
compressed line for the next smaller inventory tool. The tests inject a fake
render probe (no matplotlib) that reports scales keyed by the label's current
cutter, so the step loop, its guards, and its monotonicity are pinned
directly.
"""

from __future__ import annotations

import math
from typing import Dict, List

import pytest

from plt_optimizer.generate.cutter_downsize import apply_compression_cutter_downsize
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine

_INVENTORY = [0.03, 0.045, 0.06, 0.09, 0.125]


def _line(
    text: str = "WIDE",
    *,
    nominal: float = 0.5,
    cutter: float = 0.09,
    max_h_compress: float = 0.5,
    cutter_size: float | None = None,
    cutter_downsize: bool = True,
    max_cutter_downsizes: int = 1,
) -> ResolvedTextLine:
    """Build a resolved line with a compression budget and automatic cutter."""
    return ResolvedTextLine(
        text=text,
        nominal_text_height=nominal,
        toolpath_text_height=nominal - cutter,
        cutter_diameter=cutter,
        character_spacing=0.0,
        line_spacing=0.0,
        max_h_compress=max_h_compress,
        cutter_size=cutter_size,
        cutter_downsize=cutter_downsize,
        max_cutter_downsizes=max_cutter_downsizes,
    )


def _label(label_id: str, *lines: ResolvedTextLine) -> ResolvedLabel:
    """Build a minimal resolved label carrying the given lines."""
    return ResolvedLabel(
        id=label_id,
        count=1,
        width=4.0,
        height=2.0,
        margin=0.1,
        h_margin=0.1,
        v_margin=0.1,
        content=list(lines),
    )


def _probe(scales_by_cutter: Dict[float, float]) -> object:
    """Build a probe reporting ``scales_by_cutter`` for each line's cutter.

    The probe maps a label's per-line ``cutter_diameter`` to the effective
    scale the renderer would measure, so re-renders after a downsize observe
    the new cutter's scale.
    """

    def probe(label: ResolvedLabel) -> Dict[int, float]:
        found: Dict[int, float] = {}
        for index, line in enumerate(label.content):
            scale = scales_by_cutter.get(line.cutter_diameter)
            if scale is not None and scale < 1.0:
                found[index] = scale
        return found

    return probe


class TestNoOpGuards:
    """The pre-pass is inert (and free) outside its scope."""

    def test_no_inventory_returns_input(self) -> None:
        """Without a tools.json inventory there is no ladder to walk."""

        def probe(label: ResolvedLabel) -> Dict[int, float]:  # pragma: no cover
            raise AssertionError("probe must not run without an inventory")

        labels = [_label("l1", _line())]
        assert apply_compression_cutter_downsize(labels, None, probe=probe) == labels
        assert apply_compression_cutter_downsize(labels, [], probe=probe) == labels

    def test_no_compression_budget_skips_render(self) -> None:
        """max_h_compress == 0.0 lines can never compress: no probe, no clone."""

        def probe(label: ResolvedLabel) -> Dict[int, float]:  # pragma: no cover
            raise AssertionError("probe must not run when no line can compress")

        labels = [_label("l1", _line(max_h_compress=0.0))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        assert out == labels and out[0] is labels[0]

    def test_opt_out_line_skips_render(self) -> None:
        """cutter_downsize False (any tier) removes the line from the pass."""

        def probe(label: ResolvedLabel) -> Dict[int, float]:  # pragma: no cover
            raise AssertionError("probe must not run for opted-out lines")

        labels = [_label("l1", _line(cutter_downsize=False))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        assert out == labels and out[0] is labels[0]

    def test_zero_budget_skips_render(self) -> None:
        """max_cutter_downsizes == 0 disables the mechanism."""

        def probe(label: ResolvedLabel) -> Dict[int, float]:  # pragma: no cover
            raise AssertionError("probe must not run with a zero budget")

        labels = [_label("l1", _line(max_cutter_downsizes=0))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        assert out == labels and out[0] is labels[0]

    def test_explicit_cutter_size_line_is_never_touched(self) -> None:
        """An explicit cutter_size is a human decision, not an automatic one."""

        def probe(label: ResolvedLabel) -> Dict[int, float]:
            return {0: 0.5}

        labels = [_label("l1", _line(cutter_size=0.09))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        assert out[0] is labels[0]

    def test_uncompressed_labels_pass_through(self) -> None:
        """A probe reporting no compression leaves every label identical."""
        labels = [_label("l1", _line())]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=_probe({0.09: 1.0}))
        assert out[0] is labels[0]
        assert out[0].cutter_downsize_by_line == {}


class TestSingleStep:
    """The default budget (1) applies at most one reduction per line."""

    def test_compressed_line_downsizes(self) -> None:
        """80% compression crosses the 0.09 -> 0.06 midpoint (0.8333): 0.06 wins.

        midpoint(0.09, 0.06) = (1 + 0.06/0.09)/2 = 0.8333; a line compressed
        to 0.80 is closer to the smaller tool's ratio, so it swaps. The
        toolpath height re-renders taller (nominal - smaller cutter).
        """
        # 0.80 < 0.8333 midpoint -> downsize 0.09 -> 0.06.
        labels = [_label("l1", _line(cutter=0.09))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=_probe({0.09: 0.80}))
        line = out[0].content[0]
        assert math.isclose(line.cutter_diameter, 0.06)
        assert math.isclose(line.toolpath_text_height, 0.5 - 0.06)
        assert math.isclose(line.nominal_text_height, 0.5)
        assert out[0].cutter_downsize_by_line == {0: (0.09, 0.06)}

    def test_above_midpoint_keeps_cutter(self) -> None:
        """0.85 compression is closer to natural width: the cutter stays."""
        labels = [_label("l1", _line(cutter=0.09))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=_probe({0.09: 0.85}))
        assert out[0] is labels[0]

    def test_downsize_logs_warning(self) -> None:
        """Every geometry-altering action logs a WARNING naming label + line."""
        captured: List[str] = []

        class _Capture:
            def warning(self, msg: str, *args: object) -> None:
                captured.append(msg % args)

        import plt_optimizer.generate.cutter_downsize as cd

        original = cd.logger
        cd.logger = _Capture()  # type: ignore[assignment]
        try:
            labels = [_label("l1", _line(cutter=0.09))]
            apply_compression_cutter_downsize(labels, _INVENTORY, probe=_probe({0.09: 0.80}))
        finally:
            cd.logger = original
        assert captured
        assert "l1" in captured[0]
        assert "0.090in to 0.060in" in captured[0]

    def test_smallest_tool_has_no_ladder(self) -> None:
        """A line already on the smallest inventory tool cannot downsize."""
        labels = [_label("l1", _line(cutter=0.03))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=_probe({0.03: 0.5}))
        assert out[0] is labels[0]

    def test_toolpath_floor_stops_the_step(self) -> None:
        """A step whose toolpath height would be <= 0 is refused."""
        # Degenerate hand-built line (resolution rejects cutter >= height):
        # nominal 0.045, current 0.06 -> the 0.045 step leaves 0.0 toolpath.
        labels = [_label("l1", _line(nominal=0.045, cutter=0.06))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=_probe({0.06: 0.5}))
        assert out[0] is labels[0]

    def test_only_compressed_lines_downsize(self) -> None:
        """A multi-line label reduces only the lines that compressed."""
        labels = [
            _label(
                "l1",
                _line("A", cutter=0.09),
                _line("B", cutter=0.06),
                _line("C", cutter=0.09),
            )
        ]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=_probe({0.09: 0.80}))
        content = out[0].content
        assert math.isclose(content[0].cutter_diameter, 0.06)
        assert math.isclose(content[1].cutter_diameter, 0.06)  # untouched, no compression
        assert math.isclose(content[2].cutter_diameter, 0.06)
        assert out[0].cutter_downsize_by_line == {0: (0.09, 0.06), 2: (0.09, 0.06)}


class TestMultiStep:
    """max_cutter_downsizes > 1 cascades, re-measuring after each step."""

    def test_cascades_while_compression_deepens(self) -> None:
        """Deeper compression at each new cutter drives 0.09 -> 0.06 -> 0.045."""
        # midpoint(0.09, 0.06) = 0.8333; midpoint(0.06, 0.045) = 0.875.
        probe = _probe({0.09: 0.80, 0.06: 0.80})
        labels = [_label("l1", _line(cutter=0.09, max_cutter_downsizes=2))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        line = out[0].content[0]
        assert math.isclose(line.cutter_diameter, 0.045)
        assert math.isclose(line.toolpath_text_height, 0.5 - 0.045)
        assert out[0].cutter_downsize_by_line == {0: (0.09, 0.045)}

    def test_budget_caps_the_cascade(self) -> None:
        """A budget of 1 stops at one step even when deeper compression persists."""
        probe = _probe({0.09: 0.80, 0.06: 0.80})
        labels = [_label("l1", _line(cutter=0.09, max_cutter_downsizes=1))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        assert math.isclose(out[0].content[0].cutter_diameter, 0.06)

    def test_cascade_stops_when_compression_shallow(self) -> None:
        """A re-render that no longer compresses ends the walk immediately."""
        # 0.09 compresses to 0.80 (< 0.8333 midpoint) -> 0.06; at 0.06 the
        # line renders at natural width (probe reports nothing) -> stop.
        probe = _probe({0.09: 0.80})
        labels = [_label("l1", _line(cutter=0.09, max_cutter_downsizes=3))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        assert math.isclose(out[0].content[0].cutter_diameter, 0.06)

    def test_cascade_stops_at_midpoint(self) -> None:
        """A step whose midpoint is not crossed ends the walk."""
        # 0.09 -> 0.06 (0.80 < 0.8333); at 0.06 scale 0.95 > midpoint(0.06,0.045)=0.875.
        probe = _probe({0.09: 0.80, 0.06: 0.95})
        labels = [_label("l1", _line(cutter=0.09, max_cutter_downsizes=3))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        assert math.isclose(out[0].content[0].cutter_diameter, 0.06)


class TestMonotonicity:
    """A downsized cutter is never enlarged again within the same line."""

    def test_scale_increasing_after_downsize_never_reverts(self) -> None:
        """The walk only steps DOWN; a shallow re-measure cannot undo a step."""
        # 0.09 compresses (0.80) -> 0.06; re-measure at 0.06 is natural (1.0),
        # and even a hypothetical "compression" at 0.06 can only step DOWN.
        probe = _probe({0.09: 0.80})
        labels = [_label("l1", _line(cutter=0.09, max_cutter_downsizes=5))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        final = out[0].content[0].cutter_diameter
        assert final < 0.09
        assert math.isclose(final, 0.06)
        original, result = out[0].cutter_downsize_by_line[0]
        assert original == 0.09
        assert result < original


class TestLabelIdentity:
    """The pass preserves object identity where nothing changed."""

    def test_untouched_labels_keep_identity(self) -> None:
        """Only downsized labels become clones; siblings pass through."""
        probe = _probe({0.09: 0.80})
        keep = _label("keep", _line(cutter=0.03))  # smallest tool, no ladder
        change = _label("change", _line(cutter=0.09))
        out = apply_compression_cutter_downsize([keep, change], _INVENTORY, probe=probe)
        assert out[0] is keep
        assert out[1] is not change

    def test_probe_runs_once_per_label_id(self) -> None:
        """The initial measurement render is cached per label id."""
        calls: List[str] = []

        def probe(label: ResolvedLabel) -> Dict[int, float]:
            calls.append(label.id)
            return {0: 0.80}

        labels = [_label("dup", _line(cutter=0.09)), _label("dup", _line(cutter=0.09))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        assert calls == ["dup"]  # initial measurement cached per label id
        assert math.isclose(out[0].content[0].cutter_diameter, 0.06)
        assert math.isclose(out[1].content[0].cutter_diameter, 0.06)

    def test_empty_input(self) -> None:
        """A job with no labels is a clean no-op."""
        assert apply_compression_cutter_downsize([], _INVENTORY) == []


@pytest.mark.parametrize(
    ("cutter", "scale", "expected"),
    [
        # midpoint(0.09, 0.06) = 0.8333...
        (0.09, 0.80, 0.06),
        (0.09, 0.84, 0.09),
        # midpoint(0.06, 0.045) = 0.875
        (0.06, 0.87, 0.045),
        (0.06, 0.88, 0.06),
        # midpoint(0.125, 0.09) = 0.86
        (0.125, 0.85, 0.09),
        (0.125, 0.99, 0.125),
    ],
)
def test_midpoint_boundaries(cutter: float, scale: float, expected: float) -> None:
    """The TODO's midpoint rule holds across the inventory ladder."""
    labels = [_label("l1", _line(cutter=cutter))]
    out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=_probe({cutter: scale}))
    assert math.isclose(out[0].content[0].cutter_diameter, expected)
