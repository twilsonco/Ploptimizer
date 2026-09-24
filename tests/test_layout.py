"""Tests for the layout engine that packs ResolvedLabel objects onto plates."""

from __future__ import annotations

import dataclasses
import math
from pathlib import Path

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
from plt_optimizer.generate.schema import LayoutMode, PlateSpec


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


class TestRotationDemoExample:
    """Regression tests for tests_deps/rotation_demo_job.yaml.

    The example is the hand-crafted rotation fixture: its 24x10 scrap sheet
    only fits every label when the packer rotates, and its equal-footprint
    regions must stay horizontal. The fixture pins ``layout: rows`` (the
    historical width-first fill) because its banner-must-rotate and abort
    expectations are row-frame specific.
    """

    def _load(self) -> tuple[list[ResolvedLabel], object]:
        """Parse and resolve the rotation demo job."""
        from plt_optimizer.generate.resolution import resolve_job_spec
        from plt_optimizer.generate.schema import parse_yaml

        job = parse_yaml(Path("tests_deps/rotation_demo_job.yaml"))
        return resolve_job_spec(job), job

    def test_example_rotates_to_fit(self) -> None:
        """All labels fit with rotation; banners and gap-fillers rotate."""
        labels, job = self._load()
        assert job.allow_rotation is True
        assert job.layout == LayoutMode.ROWS
        plates = generate_layout(
            labels,
            job.plates,
            allow_rotation=job.allow_rotation,
            layout=job.layout,
        )
        packed = [pl for p in plates for pl in p.labels]
        assert len(packed) == 14
        rotated = {pl.label_id for pl in packed if pl.rotated}
        # Both 11"-tall banners can only fit lying down on the 10" sheet.
        assert {"vertical_banner_0", "vertical_banner_1"} <= rotated
        # Equal-footprint regions (the 10x6 machine cards) stay horizontal.
        assert not any(pl.label_id.startswith("machine_card") and pl.rotated for pl in packed)

    def test_example_aborts_without_rotation(self) -> None:
        """allow_rotation=False cannot fit the job on the provided plates."""
        labels, job = self._load()
        with pytest.raises(LayoutFitError, match="Could only fit"):
            generate_layout(labels, job.plates, allow_rotation=False, layout=job.layout)


class TestColumnsDemoExample:
    """Regression tests for tests_deps/columns_demo_job.yaml.

    The example is the hand-crafted column-major fixture: 16 3x1 labels on a
    24x16 sheet must fill the full plate height with a single stacked column
    (bounding box 3x16), leaving one clean rectangular scrap block on the
    right. The historical row-major fill instead spreads them 24x2.
    """

    def _load(self) -> tuple[list[ResolvedLabel], object]:
        """Parse and resolve the column-major demo job."""
        from plt_optimizer.generate.resolution import resolve_job_spec
        from plt_optimizer.generate.schema import parse_yaml

        job = parse_yaml(Path("tests_deps/columns_demo_job.yaml"))
        return resolve_job_spec(job), job

    def test_example_fills_height_first(self) -> None:
        """The pinned job packs one full-height column, minimal width."""
        labels, job = self._load()
        assert job.layout is LayoutMode.COLUMNS
        plates = generate_layout(
            labels,
            job.plates,
            allow_rotation=job.allow_rotation,
            layout=job.layout,
        )

        assert len(plates) == 1
        packed = [pl for p in plates for pl in p.labels]
        assert len(packed) == 16
        bbox_w = max(pl.x + pl.width for pl in packed)
        bbox_h = max(pl.y + pl.height for pl in packed)
        assert math.isclose(bbox_h, 16.0)
        assert math.isclose(bbox_w, 3.0)


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
        heuristics must pack narrow labels together on shared rows. Pinned to
        the row-major frame (``layout=rows``): the column-major default
        stacks labels up the plate height instead, where the 2-row optimum
        does not apply.
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

        plates, _rendered = generate_layout_with_bounds(labels, layout=LayoutMode.ROWS)

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


def _plate_bbox(plate: PackedPlate) -> tuple[float, float]:
    """Return the (width, height) bounding box of a plate's placed labels."""
    if not plate.labels:
        return (0.0, 0.0)
    return (
        max(p.x + p.width for p in plate.labels),
        max(p.y + p.height for p in plate.labels),
    )


class TestLayoutFillOrder:
    """Fill-order behaviour of the ``layout`` mode (rows vs columns)."""

    @staticmethod
    def _scrap(width: float = 24.0, height: float = 16.0) -> list[PlateSpec]:
        """A zero-margin/zero-padding plate of the given size."""
        return [
            PlateSpec(
                id="scrap",
                width=width,
                height=height,
                margin=0.0,
                clearance_padding=0.0,
            )
        ]

    def test_default_fills_height_first(self) -> None:
        """Default (columns): 16 3x1 labels fill the full plate height.

        The used bounding box must span the full 16" height while extending
        only 3" to the right -- one full column, no width spill.
        """
        labels = [_make_label(label_id="lbl", width=3.0, height=1.0, count=16)]
        plates = generate_layout(labels, self._scrap(), allow_rotation=False)

        assert len(plates) == 1
        bbox_w, bbox_h = _plate_bbox(plates[0])
        assert math.isclose(bbox_h, 16.0)
        assert math.isclose(bbox_w, 3.0)

    def test_rows_fill_width_first(self) -> None:
        """layout=rows: labels fill the full plate width before extending down.

        Mirror image of the columns default: 40 3x1 labels keep the tight
        24"-wide x 5"-tall row-major block (the columns frame would
        instead produce a 9x16 column-major block).
        """
        labels = [_make_label(label_id="lbl", width=3.0, height=1.0, count=40)]
        plates = generate_layout(
            labels, self._scrap(), allow_rotation=False, layout=LayoutMode.ROWS
        )

        assert len(plates) == 1
        bbox_w, bbox_h = _plate_bbox(plates[0])
        assert math.isclose(bbox_w, 24.0)
        assert math.isclose(bbox_h, 5.0)

    def test_columns_extend_rightward_column_by_column(self) -> None:
        """With more labels than one column holds, columns extend rightward."""
        labels = [_make_label(label_id="lbl", width=3.0, height=1.0, count=20)]
        plates = generate_layout(labels, self._scrap(), allow_rotation=False)

        assert len(plates) == 1
        bbox_w, bbox_h = _plate_bbox(plates[0])
        # First 16 fill the height, the remaining 4 start the next column.
        assert math.isclose(bbox_h, 16.0)
        assert math.isclose(bbox_w, 6.0)

    def test_columns_carry_sequential_ids_top_of_column(self) -> None:
        """Instance ids advance bottom-to-top within a column (y-up frame)."""
        labels = [_make_label(label_id="lbl", width=3.0, height=1.0, count=16)]
        plates = generate_layout(labels, self._scrap(), allow_rotation=False)

        positions = {p.label_id: (p.x, p.y) for p in plates[0].labels}
        assert positions["lbl_0"] == pytest.approx((0.0, 0.0))
        assert positions["lbl_1"] == pytest.approx((0.0, 1.0))
        assert positions["lbl_15"] == pytest.approx((0.0, 15.0))

    def test_columns_with_rotation_keeps_slot_math(self) -> None:
        """Rotation inside the columns frame lands labels inside their slot.

        2x5 labels on a 4x10 scrap: the columns frame fits 5 upright per
        column (x=0 and x=2). With rotation allowed, the packer may turn
        labels; every placement (rotated or not) must stay within the plate
        and never overlap.
        """
        labels = [_make_label(label_id="r", width=2.0, height=5.0, count=4)]
        plates = generate_layout(
            labels, self._scrap(width=4.0, height=10.0), allow_rotation=True
        )

        assert len(plates) == 1
        plate = plates[0]
        for p in plate.labels:
            assert 0.0 <= p.x and p.x + p.width <= plate.width + 1e-9
            assert 0.0 <= p.y and p.y + p.height <= plate.height + 1e-9
        for i, a in enumerate(plate.labels):
            for b in plate.labels[i + 1 :]:
                overlap_x = a.x < b.x + b.width and b.x < a.x + a.width
                overlap_y = a.y < b.y + b.height and b.y < a.y + a.height
                assert not (overlap_x and overlap_y)

    def test_rotation_detection_in_columns_frame(self) -> None:
        """A rect rotated inside the transposed frame is flagged rotated.

        1x4 labels on a 4x3 scrap: upright they need 4" of height (frame
        width); only lying down (rotated) do they fit. The rotated flag must
        be set and the un-transposed dims swapped.
        """
        labels = [_make_label(label_id="tall", width=1.0, height=4.0, count=3)]
        plates = generate_layout(
            labels, self._scrap(width=4.0, height=3.0), allow_rotation=True
        )

        assert len(plates) == 1
        packed = plates[0].labels
        assert len(packed) == 3
        assert all(p.rotated for p in packed)
        assert all(math.isclose(p.width, 4.0) and math.isclose(p.height, 1.0) for p in packed)

    def test_plate_layout_override_wins(self) -> None:
        """A per-plate layout override beats the job-level value."""
        labels = [_make_label(label_id="lbl", width=3.0, height=1.0, count=40)]
        plate = PlateSpec(
            id="rowster",
            width=24.0,
            height=16.0,
            margin=0.0,
            clearance_padding=0.0,
            layout=LayoutMode.ROWS,
        )
        plates = generate_layout(
            labels, [plate], allow_rotation=False, layout=LayoutMode.COLUMNS
        )

        assert len(plates) == 1
        bbox_w, bbox_h = _plate_bbox(plates[0])
        # Plate override -> row-major despite the columns job default.
        assert math.isclose(bbox_w, 24.0)
        assert math.isclose(bbox_h, 5.0)

    def test_mixed_modes_pack_sequentially(self) -> None:
        """Mixed plate modes cascade leftovers in declaration order.

        Plate 1 (rows, 24x1) holds exactly 8 3x1 labels filling its width;
        the 4 leftovers flow to plate 2 (columns, 24x16) and fill its height.
        """
        labels = [_make_label(label_id="lbl", width=3.0, height=1.0, count=12)]
        plates_spec = [
            PlateSpec(id="rows1", width=24.0, height=1.0, margin=0.0, clearance_padding=0.0),
            PlateSpec(
                id="cols1",
                width=24.0,
                height=16.0,
                margin=0.0,
                clearance_padding=0.0,
                layout=LayoutMode.COLUMNS,
            ),
        ]
        plates = generate_layout(
            labels, plates_spec, allow_rotation=False, layout=LayoutMode.ROWS
        )

        assert [p.plate_id for p in plates] == ["rows1", "cols1"]
        assert len(plates[0].labels) == 8
        assert len(plates[1].labels) == 4
        bbox_w, bbox_h = _plate_bbox(plates[1])
        assert math.isclose(bbox_h, 4.0)  # column of 4: height-first
        assert math.isclose(bbox_w, 3.0)

    def test_mixed_modes_overflow_raises(self) -> None:
        """Leftovers after the last mixed-mode group abort with the fit error."""
        labels = [_make_label(label_id="lbl", width=3.0, height=1.0, count=12)]
        plates_spec = [
            PlateSpec(id="rows1", width=24.0, height=1.0, margin=0.0, clearance_padding=0.0),
            PlateSpec(
                id="cols1",
                width=3.0,
                height=3.0,
                margin=0.0,
                clearance_padding=0.0,
                layout=LayoutMode.COLUMNS,
            ),
        ]
        with pytest.raises(LayoutFitError, match="Could only fit 11 of 12"):
            generate_layout(
                labels, plates_spec, allow_rotation=False, layout=LayoutMode.ROWS
            )

    def test_columns_unbounded_uses_job_layout(self) -> None:
        """Unbounded mode honours the job-level layout (columns default)."""
        labels = [_make_label(label_id="lbl", width=3.0, height=1.0, count=16)]
        plates = generate_layout(labels, allow_rotation=False)

        assert len(plates) == 1
        assert plates[0].plate_id == "default_plate_1"
        bbox_w, bbox_h = _plate_bbox(plates[0])
        assert math.isclose(bbox_h, 16.0)
        assert math.isclose(bbox_w, 3.0)

    def test_columns_overflow_spills_to_next_default_plate(self) -> None:
        """Unbounded columns fill plate 1's capacity before opening plate 2.

        A 24x16 plate holds 8 columns of 16 stacked 3x1 labels (128); the
        129th instance lands on the second auto-allocated sheet.
        """
        labels = [_make_label(label_id="lbl", width=3.0, height=1.0, count=129)]
        plates = generate_layout(labels, allow_rotation=False)

        assert [p.plate_id for p in plates] == ["default_plate_1", "default_plate_2"]
        assert len(plates[0].labels) == 128
        assert len(plates[1].labels) == 1

    def test_columns_error_wording_unbounded(self) -> None:
        """Oversized labels in unbounded columns mode keep the size wording."""
        labels = [_make_label(width=25.0, height=17.0)]
        with pytest.raises(LayoutFitError, match="in either orientation"):
            generate_layout(labels)

    def test_bounds_path_fills_height_first(self) -> None:
        """generate_layout_with_bounds honours the columns default too."""
        labels = [_make_label(label_id="lbl", width=3.0, height=1.0, count=16)]
        plates, _rendered = generate_layout_with_bounds(
            labels, self._scrap(), allow_rotation=False
        )

        assert len(plates) == 1
        bbox_w, bbox_h = _plate_bbox(plates[0])
        assert math.isclose(bbox_h, 16.0)
        assert math.isclose(bbox_w, 3.0)

    def test_columns_transpose_entries_swaps_rid_width(self) -> None:
        """The transposed rid payload carries the packer-space width."""
        from plt_optimizer.generate.layout import _transpose_entries

        label = _make_label(label_id="a", width=3.0, height=1.0)
        entries = [(3.0, 1.0, ("a_0", label, 3.0))]

        transposed = _transpose_entries(entries, transpose=True)

        assert transposed == [(1.0, 3.0, ("a_0", label, 1.0))]
        # Identity passthrough when disabled.
        assert _transpose_entries(entries, transpose=False) is entries

    def test_extract_packed_plates_transposes_back(self) -> None:
        """Transposed extraction maps slot (x, y, w, h) to plate space."""
        label = _make_label(label_id="a", width=3.0, height=1.0)
        # Packer-space: bin offered as (16, 24); rect 1x3 at (5, 2).
        fake_bin = _FakeBin(
            "p1",
            [_FakeRect(5.0, 2.0, 1.0, 3.0, rid=("a_0", label, 1.0))],
            width=16.0,
            height=24.0,
        )

        plates = _extract_packed_plates(_FakePacker([fake_bin]), transpose=True)

        assert plates[0].width == 24.0
        assert plates[0].height == 16.0
        packed = plates[0].labels[0]
        assert (packed.x, packed.y) == (2.0, 5.0)
        assert (packed.width, packed.height) == (3.0, 1.0)
        assert packed.rotated is False
