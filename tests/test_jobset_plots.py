"""Jobset plate proofs: the per-label overlay boxes.

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
from typing import Any, Dict, List

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
    JobSetPlan,
    build_jobset_plan,
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
                name=key.replace("_", " "),
                spec_path=Path(f"{key}.yaml"),
            )
            for key in job_keys
        ),
        label_jobs=ids,
    )


def _segment(x1: float, y1: float, x2: float, y2: float) -> StrokeSegment:
    return StrokeSegment(start=Coordinate(x1, y1), end=Coordinate(x2, y2), is_cutting=True)


def _record_proofs(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Capture every plate proof the export renders, without drawing it.

    :func:`plt_optimizer.generate.vectorize._write_simple_plots` imports the
    plotter lazily, so patching the plotter module's names catches it. The
    fixture plates declare a material and the export runs with the shop's
    ``job-config.json``, so proofs go through ``plot_physical_strokes`` (the
    per-pen path that carries the job key); ``plot_plt_document`` is the
    synthetic-export fallback and is captured identically.

    Returns:
        One dict per proof call: ``path`` (the PDF), ``title`` and
        ``overlay_boxes``.
    """
    from plt_optimizer.diagnostics import plotter

    calls: List[Dict[str, Any]] = []

    def _record(*args: Any, **kwargs: Any) -> None:
        calls.append(
            {
                "path": kwargs.get("output_path", args[1] if len(args) > 1 else None),
                "title": kwargs.get("title", ""),
                "overlay_boxes": kwargs.get("overlay_boxes", ()),
            }
        )

    monkeypatch.setattr(plotter, "plot_physical_strokes", _record)
    monkeypatch.setattr(plotter, "plot_plt_document", _record)
    return calls


def _proofs(calls: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Index plate-proof calls by their file stem.

    A plate proof is ``[<material>_][p<plate>_]all_<owner>.pdf`` (per-file
    previews are named ``*_txt_*`` / ``*_bh_*`` and are not plate proofs, so the
    ``all_`` marker separates them). ``<owner>`` is the batch id for a
    whole-sheet proof and the member's job id for a per-job proof, so a key
    ending ``all_<owner>`` says exactly which job that PDF was drawn for.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for call in calls:
        stem = Path(str(call["path"])).stem
        marker = stem.rfind("all_")
        if marker != 0 and stem[marker - 1] != "_":
            continue
        out[stem] = call
    return out


def _owner(stem: str) -> str:
    """The owner token of a plate-proof file stem (after the ``all_`` marker)."""
    return stem.split("all_", 1)[1]


def _proof_by_owner(proofs: Dict[str, Dict[str, Any]], owner: str) -> Dict[str, Any]:
    """Fetch one plate proof by owner token, failing loudly if it is absent."""
    hits = [call for stem, call in proofs.items() if _owner(stem) == owner]
    assert len(hits) == 1, f"expected exactly one proof for {owner!r}, got {len(hits)}"
    return hits[0]


def _plan_proofs(
    plan: JobSetPlan,
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Dict[str, Dict[str, Any]]:
    """Run the real plate-proof writer over a real jobset export.

    The export is produced with ``plots=False`` (it writes the PLTs and fills
    ``packed_plates`` / ``plate_jobs`` / ``combined_by_plate_and_job``), then
    :func:`plt_optimizer.generate.vectorize._write_simple_plots` - the function
    that owns the batch proof, the per-job proofs and the job key - is called
    on it with the plan's provenance and the plotter faked out.

    Driving the proof writer directly - with the drawing faked out and the
    export's own plot stage switched off - keeps this test about proof
    planning rather than matplotlib. It is not a workaround:
    ``_write_simple_plots`` draws from the export's own ``packed_plates`` /
    ``plate_jobs`` / ``combined_by_plate_and_job`` and takes no job spec, so
    nothing is re-packed here. The full ``export_per_cutter_plts(...,
    plots=True)`` call on a two-member, two-plate set writes 3 PLTs and
    returns 6 proofs, one of them carrying the job key; that path is exercised
    by the CLI end-to-end jobset test.

    Returns:
        The captured proofs, keyed by owner token (see :func:`_proofs`).
    """
    from plt_optimizer.generate.vectorize import _write_simple_plots, export_per_cutter_plts

    calls = _record_proofs(monkeypatch)
    export = export_per_cutter_plts(
        plan.merged_labels,
        plan.merged_plates,
        output_dir=output_dir,
        job_id=plan.jobset_id,
        name=plan.jobset_name,
        layout=plan.layout,
        allow_rotation=plan.allow_rotation,
        default_plate_clearance=plan.default_plate_clearance,
        plots=False,
        optimize=False,
        provenance=plan.provenance,
        layer_specs={context.job_key: context.spec for context in plan.contexts},
    )
    _write_simple_plots(
        output_dir,
        plan.jobset_id,
        export,
        name=plan.jobset_name,
        provenance=plan.provenance,
    )
    return _proofs(calls)


class TestPerJobProofs:
    """One plate proof per job that shares a sheet, beside the batch's own.

    A pool sheet carries several jobs' labels in one merged toolpath, so the
    batch's whole-sheet proof cannot be filed under any one job - and a shop
    fixing one job's label cannot tell which sheet to pull. The proof writer
    therefore emits, per SHARED sheet, one all-pens proof per contributing
    job: titled with that job, captioned with the jobs it shares the sheet
    with, and carrying only its own ink (no job key - the key is for a sheet
    mixing jobs). A sheet one job occupies gets exactly one proof, because the
    whole-sheet proof already is that job's proof.
    """

    def test_a_pool_batch_proves_the_batch_and_every_job_on_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two members sharing one plate -> three proofs: batch + each job."""
        plan = build_jobset_plan(Path("tests_deps/jobset_export_batch.yaml"), job_config=None)
        proofs = _plan_proofs(plan, tmp_path / "batch", monkeypatch)

        members = sorted(context.record.job_id for context in plan.contexts)
        assert len(members) == 2, "fixture must put two jobs on one sheet"
        owners = sorted(_owner(stem) for stem in proofs)
        assert owners == sorted([plan.jobset_id, *members]), owners

    def test_each_per_job_proof_is_titled_for_its_job_and_names_its_neighbours(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Titles carry the declared job name and the shared-with caption.

        The shop reads the title off the PDF, so it must use the declared
        ``name`` (not the filesystem slug), and must say the sheet is
        shared - a per-job view of a merged sheet is not the whole sheet.
        """
        plan = build_jobset_plan(Path("tests_deps/jobset_export_batch.yaml"), job_config=None)
        proofs = _plan_proofs(plan, tmp_path / "batch", monkeypatch)

        for context in plan.contexts:
            title = _proof_by_owner(proofs, context.record.job_id)["title"]
            assert context.record.display_name in title, title
            assert "shared" in title.lower(), title
            for other in plan.contexts:
                if other is not context:
                    assert other.record.display_name in title, f"{other} missing: {title}"
        # The batch proof is the whole sheet: it is not captioned as shared.
        assert "shared" not in _proof_by_owner(proofs, plan.jobset_id)["title"].lower()

    def test_a_per_job_proof_shows_one_job_and_carries_no_job_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The key belongs on the mixed sheet; a single-job view needs none.

        A per-job proof is one job's ink by construction, so drawing job boxes
        over it would key a sheet that has exactly one job on it. (That the
        BATCH proof outlines every label on the sheet, one box per label, is
        pinned by :mod:`tests.test_jobset_plots`' overlay-geometry and
        plotter-drawing tests, which drive the physical-width path with pen
        widths - the synthetic proofs here take the centerline fallback, which
        draws no key at all.)
        """
        plan = build_jobset_plan(Path("tests_deps/jobset_export_batch.yaml"), job_config=None)
        proofs = _plan_proofs(plan, tmp_path / "batch", monkeypatch)

        for context in plan.contexts:
            assert list(_proof_by_owner(proofs, context.record.job_id)["overlay_boxes"]) == []

    def test_a_solo_sheet_is_proved_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A batch mixing a shared sheet and a solo one proves each correctly.

        ``jobset_solo.yaml`` is built so member A's short sheet overflows onto
        member B's: sheet 1 carries both jobs and sheet 2 carries B alone. A
        solo sheet has a single owner WITH ink in ``combined_by_plate_and_job``,
        so without the guard a second PDF is written for it - byte-identical to
        the whole-sheet proof, and titled as if the sheet were shared with the
        only job on it. The guard is keyed on owners with content, not on the
        number of entries, which is every job in the batch.
        """
        plan = build_jobset_plan(Path("tests_deps/jobset_solo.yaml"), job_config=None)
        proofs = _plan_proofs(plan, tmp_path / "solo", monkeypatch)

        # Sheet 1 is shared: the batch's proof plus one per job.
        shared_sheet = sorted(stem for stem in proofs if stem.startswith("p01_"))
        assert len(shared_sheet) == 3, shared_sheet
        # Sheet 2 holds one job only: the batch's proof, and nothing else.
        solo_sheet = sorted(stem for stem in proofs if stem.startswith("p02_"))
        assert solo_sheet == [f"p02_all_{plan.jobset_id}"], solo_sheet
        # Only the shared sheet's per-job proofs are captioned as shared.
        for stem in shared_sheet:
            if _owner(stem) != plan.jobset_id:
                assert "shared" in proofs[stem]["title"].lower(), stem
        assert "shared" not in proofs[solo_sheet[0]]["title"].lower()


class TestJobOverlayGeometry:
    """One box per label: inside its own slot, on every plate."""

    def test_a_mixed_plate_gets_one_box_per_label(self) -> None:
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
        # Inset by the 0.05in margin: the box is inside the label's own slot
        # [1, 5] x [1, 3], not around it.
        assert boxes[1][0].x == pytest.approx(1.05)
        assert boxes[1][0].width == pytest.approx(3.9)
        assert boxes[1][1].x == pytest.approx(6.05)

    def test_a_single_job_plate_outlines_its_labels_too(self) -> None:
        """Every label on a jobset proof carries its job's outline, including
        on a sheet one job occupies alone - an outlined label is read at a
        glance, while "no box here" forces the reader to infer ownership from
        the absence of a key. A single-job RUN draws nothing: provenance is
        ``None`` there, so that path never reaches this function.
        """
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

        assert [box.label for box in boxes[1]] == ["Job Alpha"]
        assert (boxes[1][0].x, boxes[1][0].y) == pytest.approx((1.05, 1.05))
        assert (boxes[1][0].width, boxes[1][0].height) == pytest.approx((3.9, 1.9))

    def test_a_job_split_across_a_plate_is_one_box_per_label(self) -> None:
        """The boxes are per label, never unioned per job: a 9.1 x 9.1in
        rectangle around three scattered labels shows where the job's ink
        *spans*, which is not the question - each label must be identifiable.
        """
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

        # Four labels, four boxes, grouped by job in jobset-file order.
        assert [box.label for box in boxes[1]] == [
            "Job Alpha",
            "Job Alpha",
            "Job Alpha",
            "Job Beta",
        ]
        # Each box is its own label's slot pulled in by the 0.05in margin, in
        # placement order.
        assert [(b.x, b.y, b.width, b.height) for b in boxes[1]] == pytest.approx(
            [
                (1.05, 1.05, 3.9, 1.9),
                (8.05, 5.05, 1.9, 1.9),
                (2.05, 9.05, 0.9, 0.9),
                (14.05, 1.05, 2.9, 2.9),
            ]
        )
        # Explicitly not the union the old geometry produced.
        assert all((b.width, b.height) != pytest.approx((9.1, 9.1)) for b in boxes[1])

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
        # slot [3, 4.5] x [2, 8]; the box is inset 0.05in from that.
        assert (alpha.x, alpha.y) == pytest.approx((3.05, 2.05))
        assert (alpha.width, alpha.height) == pytest.approx((1.4, 5.9))

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
            (units_to_inches(slot.x_min) + 0.05, units_to_inches(slot.y_min) + 0.05)
        )

        assert (alpha.width, alpha.height) == pytest.approx(
            (
                units_to_inches(slot.x_max - slot.x_min) - 0.1,
                units_to_inches(slot.y_max - slot.y_min) - 0.1,
            )
        )
        # Spelled out: the 6in width is the vertical span of the slot.
        assert alpha.width == pytest.approx(1.4)
        assert alpha.height == pytest.approx(5.9)

    def test_the_inset_is_configurable(self) -> None:
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

        boxes = job_overlay_boxes(provenance, plates, {1: (ALPHA, BETA)}, inset=0.25)

        # Slot [1, 5] x [1, 3] pulled 0.25in in on every side.
        assert boxes[1][0].x == pytest.approx(1.25)
        assert boxes[1][0].width == pytest.approx(3.5)
        assert boxes[1][0].height == pytest.approx(1.5)

    def test_a_label_thinner_than_the_margin_degenerates_safely(self) -> None:
        """A 0.06in-tall slot cannot hold a 0.05in inset on both sides. The
        margin clamps to half the span, so the box collapses to the label's
        centre line - it must never invert to a negative size, which the
        plotter would draw as a rectangle off in the negative quadrant.
        """
        provenance = _provenance(ALPHA, BETA, ids={f"{ALPHA}::main": ALPHA, f"{BETA}::main": BETA})
        plates = [
            PackedPlate(
                plate_id="shared",
                width=24.0,
                height=16.0,
                labels=[
                    _placement(f"{ALPHA}::main", 1.0, 1.0, 4.0, 0.06, False),
                    _placement(f"{BETA}::main", 8.0, 1.0, 2.0, 2.0, False),
                ],
            )
        ]

        boxes = job_overlay_boxes(provenance, plates, {1: (ALPHA, BETA)})

        thin = boxes[1][0]
        assert thin.width == pytest.approx(3.9)  # the long axis is untouched
        assert thin.height == pytest.approx(0.0)
        assert thin.y == pytest.approx(1.03)
        assert all(box.width >= 0.0 and box.height >= 0.0 for box in boxes[1])

    def test_the_box_lies_inside_the_label_slot_not_around_it(self) -> None:
        """The key is drawn inboard of the label's border stroke: every edge of
        the box is strictly inside the slot the label fills, on both axes.
        """
        provenance = _provenance(ALPHA, BETA, ids={f"{ALPHA}::main": ALPHA, f"{BETA}::main": BETA})
        plates = [
            PackedPlate(
                plate_id="shared",
                width=24.0,
                height=16.0,
                labels=[
                    _placement(f"{ALPHA}::main", 1.0, 1.0, 4.0, 2.0, False),
                    _placement(f"{BETA}::main", 6.0, 4.0, 3.0, 3.0, True),
                ],
            )
        ]

        boxes = job_overlay_boxes(provenance, plates, {1: (ALPHA, BETA)})

        # ALPHA fills [1, 5] x [1, 3]; BETA rotated fills [6, 9] x [4, 7].
        slots = [(1.0, 1.0, 5.0, 3.0), (6.0, 4.0, 9.0, 7.0)]
        for box, (x_min, y_min, x_max, y_max) in zip(boxes[1], slots):
            assert box.x > x_min
            assert box.y > y_min
            assert box.x + box.width < x_max
            assert box.y + box.height < y_max


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

    def test_the_legend_collapses_a_job_to_one_row(self) -> None:
        """Boxes are per label; the legend is per job. Three labels of two jobs
        draw three rectangles and two rows, so a sheet of 30 labels does not
        print a 30-row key.
        """
        boxes = [
            JobOverlayBox(
                x=1.0, y=1.0, width=4.0, height=2.0, label="Job Alpha", color="tab:blue", style="--"
            ),
            JobOverlayBox(
                x=6.0, y=1.0, width=3.0, height=2.0, label="Job Alpha", color="tab:blue", style="--"
            ),
            JobOverlayBox(
                x=1.0, y=5.0, width=3.0, height=2.0, label="Job Beta", color="tab:red", style="-."
            ),
        ]

        fig = plot_physical_strokes(self._layers(), overlay_boxes=boxes)

        try:
            legend = fig.axes[0].get_legend()
            assert legend is not None
            assert [text.get_text() for text in legend.get_texts()] == ["Job Alpha", "Job Beta"]
            rectangles = [patch for patch in fig.axes[0].patches if isinstance(patch, Rectangle)]
            assert len(rectangles) == 3
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

    def test_a_box_beyond_the_ink_widens_the_limits(self) -> None:
        """The plotter's overlay axis-limit terms still have to include a box
        that falls outside the ink. Jobset geometry produces inset boxes,
        which never do; the generic API accepts caller-supplied boxes, so a
        rectangle off the ink must not be clipped off the page.
        """
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
