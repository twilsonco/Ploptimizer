"""Jobset plate proofs: the per-job overlay boxes.

A jobset plate carries several jobs, so its combined ``*_all_*`` proof draws
one dashed coloured rectangle per job plus a legend (the TODO's answer to
"which labels on this sheet belong to which job"). The geometry is computed
headless in :func:`plt_optimizer.generate.jobset.job_overlay_boxes`; the
diagnostics layer only draws what it is given. These tests pin the geometry,
the deterministic colour/style assignment, and the drawing contract (no
boxes, no legend, no change to a single-job proof).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

from plt_optimizer.core.models import Coordinate, StrokeSegment
from plt_optimizer.core.units import units_to_inches
from plt_optimizer.diagnostics.plotter import (
    PhysicalStrokeLayer,
    plot_physical_strokes,
)
from plt_optimizer.generate.jobset import (
    JOB_LEGEND_PALETTE,
    JobOverlayBox,
    JobProvenance,
    JobRecord,
    job_overlay_boxes,
    legend_style_for,
)
from plt_optimizer.generate.packing.layout import PackedLabel, PackedPlate
from plt_optimizer.generate.resolution import ResolvedLabel

# The export's composite-id namespace is the sanitized job name.
ALPHA = "Job_Alpha"
BETA = "Job_Beta"


def _resolved(label_id: str) -> ResolvedLabel:
    """A resolved label carrying exactly the id the overlay keys on."""
    return ResolvedLabel(
        id=label_id,
        count=1,
        width=4.0,
        height=2.0,
        margin=0.1,
        h_margin=0.1,
        v_margin=0.1,
        material=None,
        shape_type="rect",
        relief_engraving=False,
        content=[],
        holes=[],
    )


def _placement(label_id: str, x: float, y: float, w: float, h: float, rotated: bool) -> PackedLabel:
    """One placement on a plate, at the packer's inches-space position."""
    return PackedLabel(
        label_id=f"{label_id}_0",
        x=x,
        y=y,
        width=w,
        height=h,
        rotated=rotated,
        source_label=_resolved(label_id),
    )


def _provenance(*job_keys: str, ids: dict[str, str]) -> JobProvenance:
    """A side table over ``job_keys`` mapping composite label ids to owners.

    Args:
        *job_keys: Member keys in jobset-file order.
        ids: composite label id -> owning key.
    """
    return JobProvenance(
        jobset_id="Batch",
        jobs=tuple(
            JobRecord(
                key=key,
                job_id=key,
                job_name=key.replace("_", " "),
                spec_path=Path(f"{key}.yaml"),
            )
            for key in job_keys
        ),
        label_jobs=ids,
    )


def _segment(x1: float, y1: float, x2: float, y2: float) -> StrokeSegment:
    return StrokeSegment(start=Coordinate(x1, y1), end=Coordinate(x2, y2), is_cutting=True)


class TestJobOverlayGeometry:
    """The box is the union of a job's placements, outset, in slot space."""

    def test_a_mixed_plate_gets_one_box_per_job(self) -> None:
        provenance = _provenance(ALPHA, BETA, ids={f"{ALPHA}::main": ALPHA, f"{BETA}::main": BETA})
        plates = [
            PackedPlate(
                plate_id="shared",
                width=24.0,
                height=16.0,
                labels=[
                    _placement(f"{ALPHA}::main", 1.0, 1.0, 4.0, 2.0, False),
                    _placement(f"{BETA}::main", 6.0, 1.0, 3.0, 2.0, False),
                ],
            )
        ]

        boxes = job_overlay_boxes(provenance, plates, {1: (ALPHA, BETA)})

        assert list(boxes) == [1]
        assert [box.label for box in boxes[1]] == ["Job Alpha", "Job Beta"]
        assert boxes[1][0].x == pytest.approx(0.95)
        assert boxes[1][0].width == pytest.approx(4.1)
        assert boxes[1][1].x == pytest.approx(5.95)

    def test_a_single_job_plate_gets_no_overlay(self) -> None:
        """The proof of a plate that carries one job needs no legend, and the
        run stays byte-identical to the single-job path."""
        provenance = _provenance(ALPHA, BETA, ids={f"{ALPHA}::main": ALPHA})
        plates = [
            PackedPlate(
                plate_id="solo",
                width=24.0,
                height=16.0,
                labels=[_placement(f"{ALPHA}::main", 1.0, 1.0, 4.0, 2.0, False)],
            )
        ]

        boxes = job_overlay_boxes(provenance, plates, {1: (ALPHA,)})

        assert boxes[1] == []

    def test_a_job_split_across_a_plate_is_one_box(self) -> None:
        provenance = _provenance(
            ALPHA, BETA, ids={f"{ALPHA}::l{i}": ALPHA for i in range(3)} | {f"{BETA}::main": BETA}
        )
        plates = [
            PackedPlate(
                plate_id="shared",
                width=24.0,
                height=16.0,
                labels=[
                    _placement(f"{ALPHA}::l0", 1.0, 1.0, 4.0, 2.0, False),
                    _placement(f"{ALPHA}::l1", 8.0, 5.0, 2.0, 2.0, False),
                    _placement(f"{ALPHA}::l2", 2.0, 9.0, 1.0, 1.0, False),
                    _placement(f"{BETA}::main", 14.0, 1.0, 3.0, 3.0, False),
                ],
            )
        ]

        boxes = job_overlay_boxes(provenance, plates, {1: (ALPHA, BETA)})

        alpha = next(box for box in boxes[1] if box.label == "Job Alpha")
        assert (alpha.x, alpha.y) == pytest.approx((0.95, 0.95))
        assert (alpha.width, alpha.height) == pytest.approx((9.1, 9.1))

    def test_a_rotated_placement_boxes_its_swapped_slot(self) -> None:
        """A sideways placement fills ``[x, x+H] x [y, y+W]`` - the same swap
        :func:`plt_optimizer.generate.usage_stats._slot_rect` computes in
        device units. The proof's box must cover the ink, not the pre-rotation
        footprint."""
        provenance = _provenance(ALPHA, BETA, ids={f"{ALPHA}::main": ALPHA, f"{BETA}::main": BETA})
        plates = [
            PackedPlate(
                plate_id="shared",
                width=24.0,
                height=16.0,
                labels=[
                    _placement(f"{ALPHA}::main", 3.0, 2.0, 6.0, 1.5, True),
                    _placement(f"{BETA}::main", 2.0, 8.0, 2.0, 2.0, False),
                ],
            )
        ]

        boxes = job_overlay_boxes(provenance, plates, {1: (ALPHA, BETA)})

        alpha = next(box for box in boxes[1] if box.label == "Job Alpha")
        # Spelled out: a 6.0 x 1.5in label placed sideways fills the swapped
        # slot [3, 4.5] x [2, 8], outset by the 0.05in margin.
        assert (alpha.x, alpha.y) == pytest.approx((2.95, 1.95))
        assert (alpha.width, alpha.height) == pytest.approx((1.6, 6.1))

    def test_the_box_matches_the_usage_attribution_slot(self) -> None:
        """The proof and the usage report must agree on where a label is: the
        box is the slot rect that
        :func:`plt_optimizer.generate.usage_stats._slot_rect` computes in
        device units, converted back to inches.
        """
        from plt_optimizer.generate.usage_stats import _slot_rect

        provenance = _provenance(ALPHA, BETA, ids={f"{ALPHA}::main": ALPHA, f"{BETA}::main": BETA})
        plates = [
            PackedPlate(
                plate_id="shared",
                width=24.0,
                height=16.0,
                labels=[
                    _placement(f"{ALPHA}::main", 3.0, 2.0, 6.0, 1.5, True),
                    _placement(f"{BETA}::main", 2.0, 8.0, 2.0, 2.0, False),
                ],
            )
        ]

        boxes = job_overlay_boxes(provenance, plates, {1: (ALPHA, BETA)})

        alpha = next(box for box in boxes[1] if box.label == "Job Alpha")
        slot = _slot_rect(3.0, 2.0, 6.0, 1.5, True)
        assert (alpha.x, alpha.y) == pytest.approx(
            (units_to_inches(slot.x_min) - 0.05, units_to_inches(slot.y_min) - 0.05)
        )
        assert (alpha.width, alpha.height) == pytest.approx(
            (
                units_to_inches(slot.x_max - slot.x_min) + 0.1,
                units_to_inches(slot.y_max - slot.y_min) + 0.1,
            )
        )
        # Spelled out: the 6in width is the vertical span of the slot.
        assert alpha.width == pytest.approx(1.6)
        assert alpha.height == pytest.approx(6.1)

    def test_the_outset_is_configurable(self) -> None:
        provenance = _provenance(ALPHA, BETA, ids={f"{ALPHA}::main": ALPHA, f"{BETA}::main": BETA})
        plates = [
            PackedPlate(
                plate_id="shared",
                width=24.0,
                height=16.0,
                labels=[
                    _placement(f"{ALPHA}::main", 1.0, 1.0, 4.0, 2.0, False),
                    _placement(f"{BETA}::main", 8.0, 1.0, 2.0, 2.0, False),
                ],
            )
        ]

        boxes = job_overlay_boxes(provenance, plates, {1: (ALPHA, BETA)}, outset=0.25)

        assert boxes[1][0].x == pytest.approx(0.75)
        assert boxes[1][0].width == pytest.approx(4.5)


class TestJobOverlayIdentity:
    """Colour, style and order are deterministic and readable."""

    def test_boxes_follow_jobset_file_order(self) -> None:
        provenance = _provenance(BETA, ALPHA, ids={f"{ALPHA}::main": ALPHA, f"{BETA}::main": BETA})
        plates = [
            PackedPlate(
                plate_id="shared",
                width=24.0,
                height=16.0,
                labels=[
                    _placement(f"{ALPHA}::main", 1.0, 1.0, 4.0, 2.0, False),
                    _placement(f"{BETA}::main", 8.0, 1.0, 2.0, 2.0, False),
                ],
            )
        ]

        boxes = job_overlay_boxes(provenance, plates, {1: (ALPHA, BETA)})

        assert [box.label for box in boxes[1]] == ["Job Beta", "Job Alpha"]
        assert [box.color for box in boxes[1]] == [
            legend_style_for(0)[0],
            legend_style_for(1)[0],
        ]

    def test_the_style_pair_is_deterministic_and_cycles(self) -> None:
        assert legend_style_for(0) == ("tab:blue", "--")
        assert legend_style_for(10) == (JOB_LEGEND_PALETTE[0], "-.")
        period = len(JOB_LEGEND_PALETTE) * 3
        assert legend_style_for(7) == legend_style_for(7 + period)

    def test_every_pair_up_to_the_period_is_unique(self) -> None:
        pairs = {legend_style_for(i) for i in range(30)}
        assert len(pairs) == 30


class TestPlotterOverlayDrawing:
    """The plotter draws what it is handed, and nothing when it is not."""

    def _layers(self) -> list[PhysicalStrokeLayer]:
        return [
            PhysicalStrokeLayer(
                segments=[_segment(0, 0, 4000, 0), _segment(0, 0, 0, 6000)],
                width_inches=0.04,
                label="pen 1",
            )
        ]

    def test_overlay_boxes_draw_one_legend_row_each(self) -> None:
        boxes = [
            JobOverlayBox(
                x=1.0, y=1.0, width=4.0, height=2.0, label="Job Alpha", color="tab:blue", style="--"
            ),
            JobOverlayBox(
                x=6.0, y=1.0, width=3.0, height=2.0, label="Job Beta", color="tab:red", style="-."
            ),
        ]

        fig = plot_physical_strokes(self._layers(), overlay_boxes=boxes)

        try:
            legend = fig.axes[0].get_legend()
            assert legend is not None
            assert [text.get_text() for text in legend.get_texts()] == ["Job Alpha", "Job Beta"]
            rectangles = [patch for patch in fig.axes[0].patches if isinstance(patch, Rectangle)]
            assert len(rectangles) == 2
            assert [patch.get_linestyle() for patch in rectangles] == ["--", "-."]
            assert all(not patch.get_fill() for patch in rectangles)
        finally:
            _close(fig)

    def test_no_boxes_draws_no_legend(self) -> None:
        """The default path is unchanged: an empty overlay adds no patch and
        no legend, so a single-job proof renders exactly as before."""
        fig = plot_physical_strokes(self._layers())

        try:
            assert fig.axes[0].get_legend() is None
            assert not [
                patch for patch in fig.axes[0].patches if patch.get_linestyle() in {"--", "-.", ":"}
            ]
        finally:
            _close(fig)

    def test_an_outset_box_is_never_clipped(self) -> None:
        """The box extends past the ink by its outset; the axis limits include
        it so the rectangle is fully on the page."""
        boxes = [
            JobOverlayBox(
                x=-0.05,
                y=-0.05,
                width=6.1,
                height=1.6,
                label="Job Alpha",
                color="tab:blue",
                style="--",
            )
        ]

        fig = plot_physical_strokes(self._layers(), overlay_boxes=boxes)

        try:
            x_min, x_max = fig.axes[0].get_xlim()
            y_min, y_max = sorted(fig.axes[0].get_ylim())
            assert x_min <= -0.05
            assert x_max >= 6.05
            assert y_min <= -0.05
            assert y_max >= 1.55
        finally:
            _close(fig)

    def test_the_proof_is_written_with_overlays(self, tmp_path: Path) -> None:
        """A box list survives the PDF export path (the file the shop reads)."""
        out = tmp_path / "plate_all.pdf"
        boxes = [
            JobOverlayBox(
                x=1.0, y=1.0, width=4.0, height=2.0, label="Job Alpha", color="tab:blue", style="--"
            )
        ]

        fig = plot_physical_strokes(self._layers(), output_path=out, overlay_boxes=boxes)

        _close(fig)
        assert out.exists() and out.stat().st_size > 0


def _close(fig: Figure) -> None:
    """Close a figure (the suite shares one matplotlib state)."""
    import matplotlib.pyplot as plt

    plt.close(fig)
