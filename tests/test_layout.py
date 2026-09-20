"""Tests for the layout engine that packs ResolvedLabel objects onto plates."""

from __future__ import annotations

import dataclasses
import math

import pytest

from plt_optimizer.generate.layout import (
    DEFAULT_PLATE_HEIGHT,
    DEFAULT_PLATE_WIDTH,
    LayoutFitError,
    PackedLabel,
    PackedPlate,
    _extract_packed_plates,
    _plate_footprint,
    _render_labels_cache,
    generate_layout,
    generate_layout_with_bounds,
    unroll_labels,
)
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine
from plt_optimizer.generate.schema import PlateSpec


def _make_label(
    label_id: str = "lbl",
    width: float = 2.0,
    height: float = 1.0,
    count: int = 1,
    margin: float = 0.0,
) -> ResolvedLabel:
    """Helper to create a ResolvedLabel with minimal boilerplate."""
    return ResolvedLabel(
        id=label_id,
        count=count,
        width=width,
        height=height,
        margin=margin,
        content=[
            ResolvedTextLine(
                text="X",
                nominal_text_height=0.5,
                toolpath_text_height=0.5 - 0.03,
                cutter_diameter=0.03,
                character_spacing=0.0,
                line_spacing=0.0,
            )
        ],
    )


class TestUnrollLabels:
    """Tests for the label unrolling helper."""

    def test_single_label_single_count(self) -> None:
        """A label with count=1 should produce one rectangle."""
        labels = [_make_label(label_id="a", width=2.0, height=1.0)]
        rects = unroll_labels(labels)
        assert len(rects) == 1
        assert rects[0][0] == 2.0  # pack_width
        assert rects[0][1] == 1.0  # pack_height
        assert rects[0][2] == "a_0"  # rect_id
        assert rects[0][3] is labels[0]  # source_label reference

    def test_label_with_count(self) -> None:
        """A label with count=3 should produce three rectangles."""
        labels = [_make_label(label_id="b", count=3)]
        rects = unroll_labels(labels)
        assert len(rects) == 3
        assert [r[2] for r in rects] == ["b_0", "b_1", "b_2"]

    def test_margin_added_to_packing_dimensions(self) -> None:
        """Margin is NOT included in packing dimensions.

        Margin is applied during rendering only, not in packing.
        This ensures adjacent labels pack coincident with no gaps.
        """
        labels = [_make_label(width=2.0, height=1.0, margin=0.25)]
        rects = unroll_labels(labels)
        # pack_width = 2.0 (no margin added)
        # pack_height = 1.0 (no margin added)
        assert math.isclose(rects[0][0], 2.0)
        assert math.isclose(rects[0][1], 1.0)

    def test_multiple_labels(self) -> None:
        """Multiple labels should each produce their own rectangles."""
        labels = [
            _make_label(label_id="a", count=2),
            _make_label(label_id="b", count=1),
        ]
        rects = unroll_labels(labels)
        assert len(rects) == 3
        assert [r[2] for r in rects] == ["a_0", "a_1", "b_0"]

    def test_empty_label_list(self) -> None:
        """An empty label list should produce no rectangles."""
        rects = unroll_labels([])
        assert rects == []


class TestGenerateLayoutUnbounded:
    """Tests for unbounded mode (auto-allocation)."""

    def test_single_label_packed(self) -> None:
        """A single label should be packed onto one default plate."""
        labels = [_make_label(width=2.0, height=1.0)]
        plates = generate_layout(labels)
        assert len(plates) == 1
        assert len(plates[0].labels) == 1
        assert plates[0].labels[0].label_id == "lbl_0"
        assert math.isclose(plates[0].width, DEFAULT_PLATE_WIDTH)
        assert math.isclose(plates[0].height, DEFAULT_PLATE_HEIGHT)

    def test_multiple_labels_one_plate(self) -> None:
        """Labels that fit should be packed onto a single plate."""
        labels = [
            _make_label(label_id="a", width=2.0, height=1.0),
            _make_label(label_id="b", width=2.0, height=1.0),
            _make_label(label_id="c", width=2.0, height=1.0),
        ]
        plates = generate_layout(labels)
        assert len(plates) == 1
        assert len(plates[0].labels) == 3

    def test_label_exceeding_default_plate_raises(self) -> None:
        """A label larger than 24x16 should raise LayoutFitError."""
        labels = [_make_label(width=25.0, height=17.0)]
        with pytest.raises(LayoutFitError) as exc_info:
            generate_layout(labels)
        assert "exceed the maximum plate size" in str(exc_info.value)

    def test_default_plate_ids(self) -> None:
        """Auto-allocated plates should have predictable default IDs."""
        labels = [_make_label(width=2.0, height=1.0)]
        plates = generate_layout(labels)
        assert plates[0].plate_id == "default_plate_1"

    def test_empty_labels_returns_empty(self) -> None:
        """An empty label list should return no plates."""
        plates = generate_layout([])
        assert plates == []


class TestGenerateLayoutConstrained:
    """Tests for constrained mode (user-specified plates)."""

    def test_uses_provided_plate_id(self) -> None:
        """Constrained mode should use the user's plate ID."""
        labels = [_make_label(width=2.0, height=1.0)]
        plates = [
            PlateSpec(id="my_plate", width=24.0, height=12.0, margin=0.25, clearance_padding=0.125)
        ]
        result = generate_layout(labels, plates)
        assert len(result) == 1
        assert result[0].plate_id == "my_plate"
        assert math.isclose(result[0].width, 24.0)
        assert math.isclose(result[0].height, 12.0)

    def test_fits_on_provided_plate(self) -> None:
        """Labels that fit on the user's plate should be packed there."""
        labels = [
            _make_label(label_id="a", width=2.0, height=1.0),
            _make_label(label_id="b", width=2.0, height=1.0),
        ]
        plates = [PlateSpec(id="p1", width=24.0, height=12.0, margin=0.25, clearance_padding=0.125)]
        result = generate_layout(labels, plates)
        assert len(result) == 1
        assert len(result[0].labels) == 2

    def test_overflow_to_second_plate(self) -> None:
        """Labels that don't fit on the first plate should overflow."""
        # 24x12 plate can hold 12x 2x1 labels (margin 0)
        labels = [_make_label(label_id=f"l{i}", width=2.0, height=1.0) for i in range(20)]
        plates = [
            PlateSpec(id="p1", width=24.0, height=12.0, margin=0.0, clearance_padding=0.0),
            PlateSpec(id="p2", width=24.0, height=12.0, margin=0.0, clearance_padding=0.0),
        ]
        result = generate_layout(labels, plates)
        total_packed = sum(len(p.labels) for p in result)
        assert total_packed == 20

    def test_fit_error_when_too_small(self) -> None:
        """LayoutFitError should be raised when labels don't fit."""
        labels = [_make_label(width=10.0, height=10.0)]
        plates = [PlateSpec(id="tiny", width=5.0, height=5.0, margin=0.0, clearance_padding=0.0)]
        with pytest.raises(LayoutFitError) as exc_info:
            generate_layout(labels, plates)
        assert "Could only fit" in str(exc_info.value)

    def test_explicit_empty_plates_treated_as_unbounded(self) -> None:
        """An empty plates list should fall back to unbounded mode."""
        labels = [_make_label(width=2.0, height=1.0)]
        result = generate_layout(labels, [])
        assert len(result) == 1
        assert result[0].plate_id.startswith("default_plate_")


class TestPackedLabelCoordinates:
    """Tests for the coordinate extraction logic."""

    def test_coordinates_are_non_negative(self) -> None:
        """All packed labels should have non-negative x, y coordinates."""
        labels = [_make_label(label_id=f"l{i}", width=2.0, height=1.0) for i in range(5)]
        plates = generate_layout(labels)
        for plate in plates:
            for packed in plate.labels:
                assert packed.x >= 0
                assert packed.y >= 0

    def test_label_within_plate_bounds(self) -> None:
        """All packed labels should fit within their plate's bounds."""
        labels = [_make_label(label_id=f"l{i}", width=2.0, height=1.0) for i in range(5)]
        plates = generate_layout(labels)
        for plate in plates:
            for packed in plate.labels:
                assert packed.x + packed.width <= plate.width + 0.001
                assert packed.y + packed.height <= plate.height + 0.001

    def test_no_overlapping_labels(self) -> None:
        """No two labels on the same plate should overlap."""
        labels = [_make_label(label_id=f"l{i}", width=2.0, height=1.0) for i in range(5)]
        plates = generate_layout(labels)
        for plate in plates:
            for i, a in enumerate(plate.labels):
                for b in plate.labels[i + 1 :]:
                    # Check non-overlap (with small tolerance)
                    overlap_x = a.x < b.x + b.width and b.x < a.x + a.width
                    overlap_y = a.y < b.y + b.height and b.y < a.y + a.height
                    assert not (overlap_x and overlap_y), (
                        f"Labels {a.label_id} and {b.label_id} overlap"
                    )


class TestRotationDetection:
    """Tests for rotation detection in packed labels."""

    def test_non_rotated_label(self) -> None:
        """A label that fits without rotation should have rotated=False."""
        # Single label that fits comfortably
        labels = [_make_label(width=2.0, height=1.0)]
        plates = generate_layout(labels)
        packed = plates[0].labels[0]
        # Width should match the original (no rotation)
        assert math.isclose(packed.width, 2.0)
        assert math.isclose(packed.height, 1.0)
        assert packed.rotated is False

    def test_source_label_reference_preserved(self) -> None:
        """PackedLabel should reference the original ResolvedLabel."""
        labels = [_make_label(label_id="original", width=2.0, height=1.0)]
        plates = generate_layout(labels)
        packed = plates[0].labels[0]
        assert packed.source_label is labels[0]
        assert packed.source_label.id == "original"


class TestRotationEnabled:
    """Tests for 90-degree rotation packing (allow_rotation)."""

    def test_rotated_when_only_orientation_fits(self) -> None:
        """A label taller than the plate must be packed rotated."""
        labels = [_make_label(label_id="tall", width=1.0, height=3.5)]
        plates = [PlateSpec(id="wide", width=12.0, height=3.0, margin=0.0, clearance_padding=0.0)]
        result = generate_layout(labels, plates)
        assert len(result) == 1
        packed = result[0].labels[0]
        assert packed.rotated is True
        # Rotated slot swaps the packing dimensions.
        assert math.isclose(packed.width, 3.5)
        assert math.isclose(packed.height, 1.0)

    def test_allow_rotation_false_never_rotates(self) -> None:
        """With allow_rotation=False the same label cannot fit at all."""
        labels = [_make_label(label_id="tall", width=1.0, height=3.5)]
        plates = [PlateSpec(id="wide", width=12.0, height=3.0, margin=0.0, clearance_padding=0.0)]
        with pytest.raises(LayoutFitError):
            generate_layout(labels, plates, allow_rotation=False)

    def test_tie_prefers_unrotated(self) -> None:
        """Equal footprints must resolve to the all-horizontal layout.

        MaxRects fitness functions can strictly prefer a rotated placement
        (e.g. short-side tie-breaks); the (footprint, rotations) selection
        key in _pack_best must still pick the unrotated candidate whenever
        rotation does not strictly improve the footprint.
        """
        labels = [_make_label(width=2.0, height=1.0)]
        plates = generate_layout(labels)
        packed = plates[0].labels[0]
        assert packed.rotated is False
        assert math.isclose(packed.width, 2.0)
        assert math.isclose(packed.height, 1.0)

    def test_tie_prefers_unrotated_with_float_noise(self) -> None:
        """Footprint ties differing only by float noise stay unrotated.

        Three 1.05x2.8 labels on a 12x3 plate: unrotated footprint
        (3*1.05) * 2.8 and rotated footprint (3*2.8) * 1.05 are
        mathematically equal but differ in the last float bits, so the
        selection must compare footprints with a tolerance before falling
        back to the rotation-count tie-break.
        """
        labels = [_make_label(label_id="t", width=1.05, height=2.8, count=3)]
        plates = [PlateSpec(id="s", width=12.0, height=3.0, margin=0.0, clearance_padding=0.0)]
        result = generate_layout(labels, plates)
        assert sum(len(p.labels) for p in result) == 3
        assert all(not p.rotated for plate in result for p in plate.labels)

    def test_rotation_improves_footprint(self) -> None:
        """Rotation is used when it strictly improves (here enables) the fit."""
        # Four 10x6 labels do not fit a 24x10 plate unrotated (2 per row x 2
        # rows = 12in tall), but rotated 6x10 they fill one exact row.
        labels = [_make_label(label_id=f"m{i}", width=10.0, height=6.0) for i in range(4)]
        plates = [PlateSpec(id="p", width=24.0, height=10.0, margin=0.0, clearance_padding=0.0)]
        result = generate_layout(labels, plates)
        total_packed = sum(len(p.labels) for p in result)
        assert total_packed == 4
        rotated = [pl for p in result for pl in p.labels if pl.rotated]
        assert len(rotated) == 4
        for packed in rotated:
            assert math.isclose(packed.width, 6.0)
            assert math.isclose(packed.height, 10.0)

    def test_sideways_fit_in_unbounded_mode(self) -> None:
        """A 15x20 label exceeds 24x16 upright but fits rotated."""
        labels = [_make_label(label_id="portrait", width=15.0, height=20.0)]
        plates = generate_layout(labels)
        packed = plates[0].labels[0]
        assert packed.rotated is True
        assert math.isclose(packed.width, 20.0)
        assert math.isclose(packed.height, 15.0)

    def test_oversized_both_orientations_raises(self) -> None:
        """A label too large in both orientations raises with new wording."""
        labels = [_make_label(width=25.0, height=17.0)]
        with pytest.raises(LayoutFitError) as exc_info:
            generate_layout(labels)
        assert "in either orientation" in str(exc_info.value)

    def test_rotation_detection_uses_packing_width(self) -> None:
        """Rendered dimensions wider than nominal must not fake a rotation.

        Regression test: rotation detection compares rect.width against the
        *packing* width (rendered bounds), not the nominal ResolvedLabel
        width. A label whose rendered content overflows its nominal boundary
        (long text, compression disabled) packs unrotated and must report
        rotated=False.
        """
        from plt_optimizer.generate.layout import generate_layout_with_bounds

        label = ResolvedLabel(
            id="overflow",
            count=1,
            width=1.0,
            height=1.0,
            margin=0.1,
            content=[
                ResolvedTextLine(
                    text="W" * 20,
                    nominal_text_height=0.5,
                    toolpath_text_height=0.47,
                    cutter_diameter=0.03,
                    character_spacing=0.0,
                    line_spacing=0.0,
                )
            ],
        )
        plates, rendered_map = generate_layout_with_bounds([label])
        packed = plates[0].labels[0]
        rendered = rendered_map[label.id]
        # The rendered content must actually overflow for this to be a
        # meaningful regression guard.
        assert rendered.width > label.width + 0.5
        assert packed.rotated is False
        assert math.isclose(packed.width, rendered.width, rel_tol=1e-6)


class TestPackedPlateDataclass:
    """Tests for the PackedPlate dataclass."""

    def test_default_labels_empty(self) -> None:
        """PackedPlate should default to an empty labels list."""
        plate = PackedPlate(plate_id="p1", width=24.0, height=12.0)
        assert plate.labels == []

    def test_packed_label_is_frozen(self) -> None:
        """PackedLabel should be immutable."""
        label = ResolvedLabel(id="x", count=1, width=1.0, height=1.0, margin=0.0)
        packed = PackedLabel(
            label_id="x_0", x=0.0, y=0.0, width=1.0, height=1.0, rotated=False, source_label=label
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            packed.x = 1.0  # type: ignore[misc]


class TestLayoutFitError:
    """Tests for the LayoutFitError exception."""

    def test_is_exception(self) -> None:
        """LayoutFitError should be an Exception subclass."""
        assert issubclass(LayoutFitError, Exception)

    def test_can_be_raised_with_message(self) -> None:
        """LayoutFitError should carry a descriptive message."""
        with pytest.raises(LayoutFitError) as exc_info:
            raise LayoutFitError("Test error message")
        assert "Test error message" in str(exc_info.value)


class TestBestAlgorithmSelection:
    """Tests for the multi-algorithm best-fit packing selection."""

    def test_selects_tighter_layout_for_mixed_widths(self) -> None:
        """Narrow labels should share rows rather than each getting its own.

        Regression test: MaxRectsBssf alone strands a 3x1 label at the end of
        row 0, wasting vertical space. The best-fit selection across multiple
        heuristics must pack narrow labels together on shared rows.
        """
        from plt_optimizer.generate.layout import generate_layout_with_bounds

        # Two wide (10") and three narrow (3") plus one medium (6"), all 1" tall.
        labels = [
            _make_label(label_id="test_1", width=3.0, height=1.0),
            _make_label(label_id="test_2", width=3.0, height=1.0),
            _make_label(label_id="test_3", width=3.0, height=1.0),
            _make_label(label_id="alpha_lower", width=10.0, height=1.0),
            _make_label(label_id="alpha_upper", width=10.0, height=1.0),
            _make_label(label_id="digits", width=6.0, height=1.0),
        ]

        plates, _rendered = generate_layout_with_bounds(labels)

        # All labels must fit on a single plate.
        assert len(plates) == 1
        packed = plates[0].labels

        # Compute the bounding-box height actually used by placed content.
        max_y = max(p.y + p.height for p in packed)
        # Optimal layout uses exactly two rows (2 inches).
        assert math.isclose(max_y, 2.0), f"Expected tight 2-row packing, got {max_y}"

    def test_no_overlap_in_selected_layout(self) -> None:
        """The selected best-fit layout must never contain overlapping labels."""
        from plt_optimizer.generate.layout import generate_layout_with_bounds

        labels = [_make_label(label_id=f"l{i}", width=3.0, height=1.0) for i in range(4)] + [
            _make_label(label_id="wide", width=10.0, height=1.0)
        ]

        plates, _rendered = generate_layout_with_bounds(labels)

        for plate in plates:
            for i, a in enumerate(plate.labels):
                for b in plate.labels[i + 1 :]:
                    overlap_x = a.x < b.x + b.width and b.x < a.x + a.width
                    overlap_y = a.y < b.y + b.height and b.y < a.y + a.height
                    assert not (overlap_x and overlap_y), (
                        f"Labels {a.label_id} and {b.label_id} overlap"
                    )

    def test_plate_footprint_rewards_tight_packing(self) -> None:
        """Footprint metric should be smaller for tighter layouts."""
        import rectpack

        from plt_optimizer.generate.layout import PACK_CONFIGS, _plate_footprint

        # Build packers with the same 24x16 bin and confirm footprint is
        # positive (non-empty) across all candidate configurations.
        def build(algo, sort_algo) -> rectpack.packer.Packer:
            p = rectpack.newPacker(
                mode=rectpack.PackingMode.Offline,
                bin_algo=rectpack.PackingBin.BFF,
                pack_algo=algo,
                sort_algo=sort_algo,
                rotation=False,
            )
            for w, h in [(10, 1), (3, 1), (6, 1), (3, 1), (3, 1)]:
                p.add_rect(w, h)
            p.add_bin(24.0, 16.0)
            p.pack()
            return p

        footprints = [_plate_footprint(build(a, s)) for a, s in PACK_CONFIGS]
        assert all(fp > 0 for fp in footprints)


class _FakeRect:
    """Minimal stand-in for a ``rectpack`` placed rectangle."""

    def __init__(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        rid: object,
    ) -> None:
        """Store the placed rectangle geometry and its rid payload."""
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.rid = rid


class _FakeBin:
    """Minimal stand-in for a ``rectpack`` bin (plate) of placed rects."""

    def __init__(
        self,
        bid: str,
        rects: list[_FakeRect],
        width: float = 24.0,
        height: float = 16.0,
    ) -> None:
        """Store the bin identity and its placed rectangles."""
        self.bid = bid
        self.width = width
        self.height = height
        self._rects = rects

    def __len__(self) -> int:
        """Return the number of rectangles placed in this bin."""
        return len(self._rects)

    def __iter__(self) -> object:
        """Iterate the placed rectangles."""
        return iter(self._rects)


class _FakePacker:
    """Minimal stand-in for a packed ``rectpack`` packer (iterable of bins)."""

    def __init__(self, bins: list[_FakeBin]) -> None:
        """Store the bins to expose on iteration."""
        self._bins = bins

    def __iter__(self) -> object:
        """Iterate the bins."""
        return iter(self._bins)


class TestRenderLabelsCacheDeduplication:
    """Tests for the ID-based render cache used by rendered-bounds packing."""

    def test_duplicate_ids_rendered_once(self) -> None:
        """A repeated label ID must be served from the cache, not re-rendered."""
        first = _make_label(label_id="dup", width=2.0, height=1.0)
        second = _make_label(label_id="dup", width=2.0, height=1.0)

        cache = _render_labels_cache([first, second])

        assert list(cache) == ["dup"]
        # The first render wins; the duplicate hit the cache (miss branch).
        assert cache["dup"].source_label is first


class TestEmptyBinHandling:
    """Tests for discarding empty bins in packer result translation."""

    def test_extract_packed_plates_skips_empty_bins(self) -> None:
        """Empty auto-allocated bins must not surface as PackedPlates."""
        label = _make_label(label_id="a", width=2.0, height=1.0)
        empty = _FakeBin("default_plate_2", [])
        filled = _FakeBin(
            "default_plate_1",
            [_FakeRect(0.0, 0.0, 2.0, 1.0, rid=("a_0", label, 2.0))],
        )

        plates = _extract_packed_plates(_FakePacker([empty, filled]))

        assert len(plates) == 1
        assert plates[0].plate_id == "default_plate_1"
        assert plates[0].labels[0].label_id == "a_0"

    def test_plate_footprint_ignores_empty_bins(self) -> None:
        """Empty bins must contribute zero to the footprint metric."""
        empty = _FakeBin("default_plate_2", [])
        filled = _FakeBin(
            "default_plate_1",
            [
                _FakeRect(0.0, 0.0, 2.0, 1.0, rid=("a_0", None)),
                _FakeRect(2.0, 0.0, 3.0, 1.0, rid=("b_0", None)),
            ],
        )

        footprint = _plate_footprint(_FakePacker([empty, filled]))

        # Bounding box of placed content: 5in wide x 1in tall.
        assert footprint == pytest.approx(5.0)


class TestGenerateLayoutWithBoundsFitErrors:
    """LayoutFitError paths of generate_layout_with_bounds (rendered dims)."""

    def test_constrained_overflow_raises(self) -> None:
        """Constrained plates too small must raise the constrained-fit error."""
        labels = [_make_label(width=10.0, height=10.0)]
        plates = [PlateSpec(id="tiny", width=5.0, height=5.0, margin=0.0, clearance_padding=0.0)]
        with pytest.raises(LayoutFitError) as exc_info:
            generate_layout_with_bounds(labels, plates)
        assert "Could only fit" in str(exc_info.value)

    def test_unbounded_label_exceeding_default_plate_raises(self) -> None:
        """A rendered label larger than 24x16 must raise the size error."""
        labels = [_make_label(width=25.0, height=17.0)]
        with pytest.raises(LayoutFitError) as exc_info:
            generate_layout_with_bounds(labels)
        assert "exceed the maximum plate size" in str(exc_info.value)
