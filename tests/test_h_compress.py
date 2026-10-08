"""Unit tests for the per-label shared horizontal compression pre-pass.

``plt_optimizer.generate.h_compress`` renders each label once, reads the
effective per-line horizontal scales, and where one line of a same-height
group was compressed, applies the group's most-compressed (minimum) scale to
every other eligible line of that group. The tests inject a fake render probe
(no matplotlib) reporting scales keyed by line index, so the grouping, the
budget clamping, and the eligibility guards are pinned directly.
"""

from __future__ import annotations

from typing import Dict, List

import pytest

from plt_optimizer.generate.h_compress import (
    _is_eligible,
    _label_has_candidates,
    _shared_scale,
    apply_global_h_compress,
)
from plt_optimizer.generate.resolution import (
    DEFAULT_H_COMPRESS_GLOBAL,
    ResolvedLabel,
    ResolvedTextLine,
)


def _line(
    text: str = "WIDE",
    *,
    nominal: float = 0.5,
    cutter: float = 0.06,
    max_h_compress: float = 0.5,
    h_compress_global: bool = True,
) -> ResolvedTextLine:
    """Build a resolved line with a compression budget and sharing permission."""
    return ResolvedTextLine(
        text=text,
        nominal_text_height=nominal,
        toolpath_text_height=nominal - cutter,
        cutter_diameter=cutter,
        character_spacing=0.0,
        line_spacing=0.0,
        max_h_compress=max_h_compress,
        h_compress_global=h_compress_global,
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


def _probe(scales_by_line: Dict[int, float]) -> object:
    """Build a probe reporting a fixed per-line scale map for any label.

    Args:
        scales_by_line: Effective scale per line index (only entries below
            ``1.0`` are treated as compressed).

    Returns:
        A callable matching :data:`plt_optimizer.generate.cutter_downsize.ScaleProbe`.
    """

    def probe(label: ResolvedLabel) -> Dict[int, float]:
        return {
            index: scale
            for index, scale in scales_by_line.items()
            if index < len(label.content) and scale < 1.0
        }

    return probe


class TestEligibility:
    """The eligibility predicate gates every sharing decision."""

    def test_default_module_constant_is_false(self) -> None:
        """The shipped default keeps compression per-line."""
        assert DEFAULT_H_COMPRESS_GLOBAL is False

    def test_flag_and_budget_required(self) -> None:
        """Sharing needs the permission on and a compression budget > 0."""
        assert _is_eligible(_line()) is True
        assert _is_eligible(_line(h_compress_global=False)) is False
        assert _is_eligible(_line(max_h_compress=0.0)) is False

    def test_label_candidate_scan(self) -> None:
        """A label has candidates only when one of its lines is eligible."""
        assert _label_has_candidates(_label("a", _line())) is True
        assert (
            _label_has_candidates(_label("a", _line(h_compress_global=False))) is False
        )


class TestSharedScaleClamp:
    """The group scale is clamped to each receiver's own budget floor."""

    def test_group_scale_used_when_inside_budget(self) -> None:
        """A budget deeper than the group scale passes it through."""
        assert _shared_scale(_line(max_h_compress=0.5), 0.8) == pytest.approx(0.8)

    def test_budget_floor_wins_when_shallower(self) -> None:
        """A shallower budget keeps the line at its own floor."""
        assert _shared_scale(_line(max_h_compress=0.1), 0.5) == pytest.approx(0.9)

    def test_negative_budget_clamped(self) -> None:
        """A nonsensical negative budget clamps to the 1.0 floor."""
        assert _shared_scale(_line(max_h_compress=-1.0), 0.5) == pytest.approx(1.0)


class TestNoOpGuards:
    """The pass costs nothing and changes nothing when it cannot fire."""

    def test_no_eligible_line_is_identity(self) -> None:
        """A job without a sharing line returns the same label objects."""
        labels = [_label("a", _line(h_compress_global=False))]

        def probe(label: ResolvedLabel) -> Dict[int, float]:  # pragma: no cover
            raise AssertionError("probe must not run when nothing is eligible")

        out = apply_global_h_compress(labels, probe=probe)
        assert out[0] is labels[0]

    def test_no_compression_is_identity(self) -> None:
        """A label whose lines all render at natural width is untouched."""
        labels = [_label("a", _line("WIDE"), _line("OK"))]
        out = apply_global_h_compress(labels, probe=_probe({}))
        assert out[0] is labels[0]
        assert out[0].global_compress_by_line == {}

    def test_single_eligible_line_never_shares(self) -> None:
        """A group of one has no sibling to share with."""
        labels = [_label("a", _line("WIDE"))]
        out = apply_global_h_compress(labels, probe=_probe({0: 0.7}))
        assert out[0] is labels[0]


class TestSharing:
    """A trigger's scale lands on its same-height siblings."""

    def test_fitting_sibling_receives_the_group_scale(self) -> None:
        """The trigger and its fitting sibling converge to the same scale."""
        label = _label("shared", _line("LONG TEXT"), _line("OK"))

        out = apply_global_h_compress([label], probe=_probe({0: 0.8}))

        shared = out[0].global_compress_by_line
        assert sorted(shared) == [0, 1]
        assert shared[0] == pytest.approx(0.8)
        assert shared[1] == pytest.approx(0.8)

    def test_group_converges_to_the_most_compressed(self) -> None:
        """Two triggers at different scales share the minimum."""
        label = _label("g", _line("A"), _line("B"), _line("C"))

        out = apply_global_h_compress([label], probe=_probe({0: 0.9, 1: 0.7}))

        shared = out[0].global_compress_by_line
        assert sorted(shared) == [0, 1, 2]
        assert all(scale == pytest.approx(0.7) for scale in shared.values())

    def test_receiver_budget_floor_wins(self) -> None:
        """A sibling with a shallower budget lands on its own floor."""
        label = _label(
            "g",
            _line("TRIGGER", max_h_compress=0.5),
            _line("TIGHT", max_h_compress=0.1),
        )

        out = apply_global_h_compress([label], probe=_probe({0: 0.5}))

        shared = out[0].global_compress_by_line
        assert shared[0] == pytest.approx(0.5)
        assert shared[1] == pytest.approx(0.9)

    def test_receiver_at_its_floor_is_omitted(self) -> None:
        """A sibling whose floor is 1.0 is not listed as compressed."""
        label = _label(
            "g",
            _line("TRIGGER", max_h_compress=0.5),
            _line("MID", max_h_compress=0.5),
            _line("NONE", max_h_compress=0.0),
        )

        out = apply_global_h_compress([label], probe=_probe({0: 0.5}))

        shared = out[0].global_compress_by_line
        assert sorted(shared) == [0, 1]

    def test_sharing_never_crosses_the_label_boundary(self) -> None:
        """A fitting line in another label keeps its natural width."""
        labels = [
            _label("shared", _line("LONG TEXT"), _line("OK")),
            _label("other", _line("OK")),
        ]

        def probe(label: ResolvedLabel) -> Dict[int, float]:
            if label.id != "shared":
                return {}
            return {0: 0.8}

        out = apply_global_h_compress(labels, probe=probe)

        assert sorted(out[0].global_compress_by_line) == [0, 1]
        assert out[1] is labels[1]
        assert out[1].global_compress_by_line == {}

    def test_other_text_height_is_untouched(self) -> None:
        """Sharing is scoped to one nominal text height."""
        label = _label(
            "g",
            _line("WIDE", nominal=0.5),
            _line("OK", nominal=0.5),
            _line("SMALL", nominal=0.25),
            _line("SMALL2", nominal=0.25),
        )

        out = apply_global_h_compress([label], probe=_probe({0: 0.8, 2: 0.6}))

        shared = out[0].global_compress_by_line
        assert sorted(shared) == [0, 1, 2, 3]
        assert shared[0] == pytest.approx(0.8)
        assert shared[1] == pytest.approx(0.8)
        assert shared[2] == pytest.approx(0.6)
        assert shared[3] == pytest.approx(0.6)

    def test_trigger_opt_out_stays_per_line(self) -> None:
        """h_compress_global false on the trigger keeps it per-line."""
        label = _label(
            "g",
            _line("WIDE", h_compress_global=False),
            _line("OK"),
            _line("OK2"),
        )

        out = apply_global_h_compress([label], probe=_probe({0: 0.8}))

        # The opted-out trigger's natural compression is its own business; the
        # eligible group has no trigger, so nothing is shared.
        assert out[0] is label
        assert out[0].global_compress_by_line == {}

    def test_receiver_opt_out_is_never_touched(self) -> None:
        """h_compress_global false on a sibling blocks the propagation."""
        label = _label(
            "g",
            _line("WIDE"),
            _line("MID"),
            _line("OK", h_compress_global=False),
        )

        out = apply_global_h_compress([label], probe=_probe({0: 0.8}))

        shared = out[0].global_compress_by_line
        assert sorted(shared) == [0, 1]

    def test_ineligible_sibling_is_never_a_receiver(self) -> None:
        """A zero-budget line neither triggers nor receives."""
        label = _label(
            "g",
            _line("WIDE", max_h_compress=0.5),
            _line("MID", max_h_compress=0.5),
            _line("BUDGETLESS", max_h_compress=0.0),
        )

        out = apply_global_h_compress([label], probe=_probe({0: 0.8}))

        shared = out[0].global_compress_by_line
        assert sorted(shared) == [0, 1]

    def test_flag_off_is_bit_identical(self) -> None:
        """A job with the flag off returns the same objects, never rendering."""
        labels = [_label("a", _line(h_compress_global=False), _line(h_compress_global=False))]

        def probe(label: ResolvedLabel) -> Dict[int, float]:  # pragma: no cover
            raise AssertionError("probe must not run when the flag is off")

        out = apply_global_h_compress(labels, probe=probe)
        assert out[0] is labels[0]


class TestProbeCaching:
    """One measurement render per unique label id."""

    def test_probe_runs_once_per_label_id(self) -> None:
        """Repeated label ids (count > 1) share a single measurement."""
        labels = [_label("dup", _line("WIDE"), _line("OK"))] * 2
        calls: List[str] = []

        def probe(label: ResolvedLabel) -> Dict[int, float]:
            calls.append(label.id)
            return {0: 0.8}

        out = apply_global_h_compress(labels, probe=probe)

        assert calls == ["dup"]
        assert all(sorted(label.global_compress_by_line) == [0, 1] for label in out)
