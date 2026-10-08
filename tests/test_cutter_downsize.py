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

from plt_optimizer.generate.cutter_downsize import (
    _reduce_line,
    apply_compression_cutter_downsize,
)
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
    cutter_downsize_global: bool = True,
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
        cutter_downsize_global=cutter_downsize_global,
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


def _probe_by_line(scales_by_line: Dict[int, Dict[float, float]]) -> object:
    """Build a probe keyed by line index, then by that line's cutter.

    Lets a test compress one line while its siblings render at natural
    width, so a sibling's swap can only come from global sharing.
    """

    def probe(label: ResolvedLabel) -> Dict[int, float]:
        found: Dict[int, float] = {}
        for index, line in enumerate(label.content):
            scale = scales_by_line.get(index, {}).get(line.cutter_diameter)
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


class TestGlobalSharing:
    """cutter_downsize_global shares a trigger's swap across same-height siblings.

    The default (True) propagates a downsized line's final cutter to every
    other eligible line of the same nominal text height *within the same
    label*; receivers skip the midpoint trigger check, then run their own
    one-way loop.
    """

    def test_sibling_of_same_height_receives_the_swap(self) -> None:
        """A fitting sibling line adopts the trigger's final cutter."""
        # Only line 0 compresses (0.80 < midpoint 0.8333) -> 0.06; line 1
        # renders at natural width, so its swap can only come from sharing.
        probe = _probe_by_line({0: {0.09: 0.80}})
        labels = [_label("l1", _line("WIDE", cutter=0.09), _line("OK", cutter=0.09))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        content = out[0].content
        assert math.isclose(content[0].cutter_diameter, 0.06)
        assert math.isclose(content[1].cutter_diameter, 0.06)
        assert math.isclose(content[1].toolpath_text_height, 0.5 - 0.06)
        assert math.isclose(content[1].nominal_text_height, 0.5)
        assert out[0].cutter_downsize_by_line == {0: (0.09, 0.06), 1: (0.09, 0.06)}

    def test_sharing_is_scoped_to_the_label(self) -> None:
        """A same-height line in a *different* label is never touched."""

        def probe(label: ResolvedLabel) -> Dict[int, float]:
            return {0: 0.80} if label.id == "a" else {}

        trigger = _label("a", _line(cutter=0.09))
        sibling = _label("b", _line(cutter=0.09))
        out = apply_compression_cutter_downsize([trigger, sibling], _INVENTORY, probe=probe)
        assert math.isclose(out[0].content[0].cutter_diameter, 0.06)
        assert out[1] is sibling
        assert math.isclose(out[1].content[0].cutter_diameter, 0.09)

    def test_other_text_height_is_untouched(self) -> None:
        """Sharing only ever joins lines of the same nominal text height."""
        probe = _probe_by_line({0: {0.09: 0.80}})
        labels = [
            _label(
                "l1",
                _line("WIDE", nominal=0.5, cutter=0.09),
                _line("TALL", nominal=0.75, cutter=0.09),
            )
        ]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        content = out[0].content
        assert math.isclose(content[0].cutter_diameter, 0.06)
        assert math.isclose(content[1].cutter_diameter, 0.09)
        assert out[0].cutter_downsize_by_line == {0: (0.09, 0.06)}

    def test_seed_step_consumes_the_receiver_budget(self) -> None:
        """The shared swap is a real step: it spends one unit of the receiver's budget."""
        # Trigger (budget 1): 0.09 -> 0.06. Receiver (budget 1) gets the seed
        # swap to 0.06 and its budget is spent, so its own deepened
        # compression at 0.06 cannot drive a further step.
        probe = _probe_by_line({0: {0.09: 0.80}, 1: {0.06: 0.80}})
        labels = [
            _label(
                "l1",
                _line("WIDE", cutter=0.09, max_cutter_downsizes=1),
                _line("OK", cutter=0.09, max_cutter_downsizes=1),
            )
        ]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        content = out[0].content
        assert math.isclose(content[0].cutter_diameter, 0.06)
        assert math.isclose(content[1].cutter_diameter, 0.06)
        assert math.isclose(content[1].toolpath_text_height, 0.5 - 0.06)
        assert out[0].cutter_downsize_by_line == {0: (0.09, 0.06), 1: (0.09, 0.06)}

    def test_receiver_shares_the_triggers_nominal_height(self) -> None:
        """Grouping is by nominal height, so the toolpath floor is shared too.

        A degenerate group whose shared tool leaves no material on the
        receiver keeps the receiver's tool (the floor check runs on the
        receiver's own geometry, not the trigger's).
        """
        probe = _probe_by_line({0: {0.09: 0.80}})
        labels = [
            _label(
                "l1",
                _line("WIDE", nominal=0.5, cutter=0.09),
                # Same nominal (grouped) but too short for the shared tool.
                _line("TINY", nominal=0.5, cutter=0.09),
            )
        ]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        assert math.isclose(out[0].content[0].cutter_diameter, 0.06)
        assert math.isclose(out[0].content[1].cutter_diameter, 0.06)

    def test_receiver_continues_its_own_loop(self) -> None:
        """After the shared swap the receiver keeps stepping on its own budget."""
        # Only line 0 compresses at 0.09 (budget 1) -> 0.06. The receiver
        # (budget 2) gets the seed swap to 0.06, then its own deepened
        # compression at 0.06 drives it to 0.045 -- and the fixpoint pulls
        # the trigger down to the group's new minimum too, so the label ends
        # on a single tool.
        probe = _probe_by_line({0: {0.09: 0.80}, 1: {0.06: 0.80}})
        labels = [
            _label(
                "l1",
                _line("WIDE", cutter=0.09, max_cutter_downsizes=1),
                _line("OK", cutter=0.09, max_cutter_downsizes=2),
            )
        ]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        content = out[0].content
        assert math.isclose(content[0].cutter_diameter, 0.045)
        assert math.isclose(content[1].cutter_diameter, 0.045)
        assert math.isclose(content[1].toolpath_text_height, 0.5 - 0.045)
        assert out[0].cutter_downsize_by_line == {0: (0.09, 0.045), 1: (0.09, 0.045)}

    def test_group_converges_to_the_smallest_cutter(self) -> None:
        """The whole height group lands on the deepest final cutter."""
        # The trigger walks 0.09 -> 0.06 -> 0.045 -> 0.03 (budget 3); the
        # fitting sibling follows all the way to the group's final 0.03.
        probe = _probe_by_line({0: {0.09: 0.80, 0.06: 0.80, 0.045: 0.80}})
        labels = [
            _label(
                "l1",
                _line("WIDE", cutter=0.09, max_cutter_downsizes=3),
                _line("OK", cutter=0.09, max_cutter_downsizes=3),
            )
        ]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        diameters = [line.cutter_diameter for line in out[0].content]
        assert all(math.isclose(d, 0.03) for d in diameters)
        assert out[0].cutter_downsize_by_line == {0: (0.09, 0.03), 1: (0.09, 0.03)}

    def test_trigger_opt_out_shares_nothing(self) -> None:
        """cutter_downsize_global false on the trigger keeps it per-line."""
        probe = _probe_by_line({0: {0.09: 0.80}})
        labels = [
            _label(
                "l1",
                _line("WIDE", cutter=0.09, cutter_downsize_global=False),
                _line("OK", cutter=0.09),
            )
        ]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        content = out[0].content
        assert math.isclose(content[0].cutter_diameter, 0.06)
        assert math.isclose(content[1].cutter_diameter, 0.09)
        assert out[0].cutter_downsize_by_line == {0: (0.09, 0.06)}

    def test_receiver_opt_out_keeps_its_tool(self) -> None:
        """cutter_downsize_global false on a sibling blocks the propagation."""
        probe = _probe_by_line({0: {0.09: 0.80}})
        labels = [
            _label(
                "l1",
                _line("WIDE", cutter=0.09),
                _line("OK", cutter=0.09, cutter_downsize_global=False),
            )
        ]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        content = out[0].content
        assert math.isclose(content[0].cutter_diameter, 0.06)
        assert math.isclose(content[1].cutter_diameter, 0.09)
        assert out[0].cutter_downsize_by_line == {0: (0.09, 0.06)}

    def test_ineligible_siblings_are_never_receivers(self) -> None:
        """Explicit-cutter, opted-out and budget-less lines keep their tool."""
        probe = _probe_by_line({0: {0.09: 0.80}})
        labels = [
            _label(
                "l1",
                _line("WIDE", cutter=0.09),
                _line("EXPLICIT", cutter=0.125, cutter_size=0.125),
                _line("OPTOUT", cutter=0.125, cutter_downsize=False),
                _line("NOBUDGET", cutter=0.125, max_cutter_downsizes=0),
                _line("NOCOMPRESS", cutter=0.125, max_h_compress=0.0),
            )
        ]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        content = out[0].content
        assert math.isclose(content[0].cutter_diameter, 0.06)
        for line in content[1:]:
            assert math.isclose(line.cutter_diameter, 0.125)
        assert set(out[0].cutter_downsize_by_line) == {0}

    def test_group_minimum_ignores_ineligible_lines(self) -> None:
        """An untouched explicit-cutter line never drags the group down."""
        # The 0.125 explicit line sits in the same height group; its tool is
        # not part of the group's minimum, and it does not follow the group.
        probe = _probe_by_line({0: {0.09: 0.80}})
        labels = [
            _label(
                "l1",
                _line("WIDE", cutter=0.09),
                _line("EXPLICIT", cutter=0.125, cutter_size=0.125),
            )
        ]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        content = out[0].content
        assert math.isclose(content[0].cutter_diameter, 0.06)
        assert math.isclose(content[1].cutter_diameter, 0.125)

    def test_flag_off_is_bit_identical_to_per_line_behaviour(self) -> None:
        """Disabling sharing reproduces the historical (pre-sharing) result."""
        probe = _probe_by_line({0: {0.09: 0.80}})
        labels = [
            _label(
                "l1",
                _line("WIDE", cutter=0.09, cutter_downsize_global=False),
                _line("OK", cutter=0.09, cutter_downsize_global=False),
            )
        ]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        content = out[0].content
        assert math.isclose(content[0].cutter_diameter, 0.06)
        assert math.isclose(content[1].cutter_diameter, 0.09)

    def test_sharing_never_raises_a_cutter(self) -> None:
        """A group whose trigger lands *below* a sibling only pulls it down."""
        # Multi-rung trigger (0.09 -> 0.06 -> 0.045) with a sibling that
        # already sits at 0.06: it follows to 0.045, never back up to 0.09.
        probe = _probe({0.09: 0.80, 0.06: 0.80})
        labels = [
            _label(
                "l1",
                _line("WIDE", cutter=0.09, max_cutter_downsizes=2),
                _line("MID", cutter=0.06, max_cutter_downsizes=2),
            )
        ]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        diameters = [line.cutter_diameter for line in out[0].content]
        assert all(math.isclose(d, 0.045) for d in diameters)

    def test_seed_at_or_below_current_cutter_is_a_noop(self) -> None:
        """``_reduce_line`` refuses a forced cutter that is not strictly smaller.

        ``_share_downsizes`` pre-filters receivers, so this guard is reachable
        only through a direct call; it pins the seed step's contract (the
        shared tool must be a genuine step down).
        """
        label = _label("l1", _line(cutter=0.06))
        working, delta = _reduce_line(
            label, 0, _probe_by_line({0: {0.06: 0.50}}), _INVENTORY, 0.50, 0.06
        )
        assert working is label
        assert delta is None

    def test_propagation_logs_warning(self) -> None:
        """A shared swap is a geometry change: WARNING naming label + line."""
        captured: List[str] = []

        class _Capture:
            def warning(self, msg: str, *args: object) -> None:
                captured.append(msg % args)

        import plt_optimizer.generate.cutter_downsize as cd

        original = cd.logger
        cd.logger = _Capture()  # type: ignore[assignment]
        try:
            labels = [_label("l1", _line("WIDE", cutter=0.09), _line("OK", cutter=0.09))]
            apply_compression_cutter_downsize(
                labels, _INVENTORY, probe=_probe_by_line({0: {0.09: 0.80}})
            )
        finally:
            cd.logger = original
        sharing = [message for message in captured if "sharing the label's cutter" in message]
        assert len(sharing) == 1
        assert "'OK'" in sharing[0]
        assert "0.090in to 0.060in" in sharing[0]

    def test_no_trigger_means_no_sharing_cost(self) -> None:
        """A label with no downsized line is returned untouched."""

        def probe(label: ResolvedLabel) -> Dict[int, float]:
            return {1: 0.99}  # compressed, but above every midpoint

        labels = [_label("l1", _line("WIDE", cutter=0.09), _line("OK", cutter=0.09))]
        out = apply_compression_cutter_downsize(labels, _INVENTORY, probe=probe)
        assert out[0] is labels[0]


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
