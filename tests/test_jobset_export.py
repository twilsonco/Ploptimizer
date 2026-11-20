"""Jobset export tests: a shared plate is written as one toolpath per job.

A jobset packs every member's labels onto shared plates, so one plate's
per-cutter files carry several jobs. The contract under test (the export
phase of the jobset plan):

* a plate is segmented by owning job (jobset-file order) and **each segment
  is written as its own per-cutter file**, named for the job whose strokes it
  carries - cross-job routing is deliberately not done, because a merged
  stroke would belong to two jobs and stop being per-job reportable;
* a segment is byte-for-byte the toolpath that job produces on its own
  (measured order-insensitively: the shared pool places a job's labels
  elsewhere on the sheet, which shifts every coordinate);
* the per-segment header block comes from the owning member's spec, so each
  job's engraver parameters ride with its own strokes;
* :attr:`PerCutterExport.plate_jobs` / :attr:`PerCutterExport.file_jobs`
  record the ownership the report, the manifest and the previews need, and
  are empty for a single-job run (the inert-path contract).
"""

from dataclasses import replace
from pathlib import Path
from typing import Counter, Dict, List, Optional, Tuple

import pytest

from plt_optimizer.core.parser import PLTParser
from plt_optimizer.generate.job_config import JobConfig, load_job_config
from plt_optimizer.generate.jobset import (
    JobSetPlan,
    ResolvedJobContext,
    build_jobset_plan,
    normalize_pinning,
)
from plt_optimizer.generate.vectorize import PerCutterExport, export_per_cutter_plts

FIXTURES = Path("tests_deps")
REPO_CONFIG = Path("job-config.json")

ALPHA = "Export_Alpha"
BETA = "Export_Beta"


def _plan(name: str) -> JobSetPlan:
    """Build a jobset plan from a fixture in ``tests_deps/``."""
    return build_jobset_plan(FIXTURES / name, job_config=None)


def _export_batch(
    plan: JobSetPlan,
    output_dir: Path,
    job_config: Optional[JobConfig] = None,
    logger: Optional[object] = None,
) -> PerCutterExport:
    """Export a whole jobset plan (segmented, no plots, no routing)."""
    return export_per_cutter_plts(
        plan.merged_labels,
        plan.merged_plates,
        output_dir=output_dir,
        job_id=plan.jobset_id,
        job_name=plan.jobset_name,
        layout=plan.layout,
        allow_rotation=plan.allow_rotation,
        default_plate_clearance=plan.default_plate_clearance,
        plots=False,
        optimize=False,
        provenance=plan.provenance,
        layer_specs={context.job_key: context.spec for context in plan.contexts},
        job_config=job_config,
        job_spec=plan.contexts[0].spec if job_config is not None else None,
        logger=logger,  # type: ignore[arg-type]
    )


def _export_alone(context: ResolvedJobContext, output_dir: Path) -> PerCutterExport:
    """Export one member by itself - the reference for segment equality."""
    return export_per_cutter_plts(
        list(context.resolved_labels),
        list(context.plates) if context.plates is not None else None,
        output_dir=output_dir,
        job_id=context.record.job_id,
        job_name=context.record.display_name,
        layout=context.layout,
        allow_rotation=context.allow_rotation,
        default_plate_clearance=context.default_plate_clearance,
        plots=False,
        optimize=False,
    )


def _cutting_points(stroke: object) -> List[List[Tuple[int, int]]]:
    """Return the pen-down polylines of one parsed stroke, in device units.

    Args:
        stroke: A :class:`~plt_optimizer.core.models.StrokePath`.

    Returns:
        One list of ``(x, y)`` points per cutting segment.
    """
    polylines: List[List[Tuple[int, int]]] = []
    for segment in getattr(stroke, "segments", ()):
        if not segment.is_cutting:
            continue
        polylines.append(
            [
                (round(segment.start.x), round(segment.start.y)),
                (round(segment.end.x), round(segment.end.y)),
            ]
        )
    return polylines


def _normalized(signature: Counter[Tuple[int, int]]) -> Counter[Tuple[int, int]]:
    """Canonicalize a step multiset under the 90-degree rotation group.

    A shared pool is free to turn a label on the sheet (and this batch's
    members are all turned, to fit the sheet), and a turn is a rigid
    rotation: the 90-degree orbit of a vector ``(dx, dy)`` is
    ``{(dx, dy), (-dy, dx), (-dx, -dy), (dy, -dx)}``. Collapsing every step
    onto the lexicographically smallest member of its orbit makes the
    signature equal for the same toolpath in any orientation, which is the
    invariant worth pinning - stroke-for-stroke orientation is a packing
    decision, not a geometry change.

    Args:
        signature: The anchored step counts from :func:`_segments`.

    Returns:
        The orbit-canonical step counts.
    """
    canonical: Counter[Tuple[int, int]] = Counter()
    for (dx, dy), count in signature.items():
        orbit = {(dx, dy), (-dy, dx), (-dx, -dy), (dy, -dx)}
        key = min(orbit)
        canonical[key] += count
    return canonical


def _segments(paths: List[Path]) -> Counter[Tuple[int, int]]:
    """Translation/rotation-invariant geometry signature of a set of PLT files.

    Every cutting vector is anchored at its own start and counted as a
    ``(dx, dy)`` step, then canonicalized over its rotation orbit, so the
    signature of the same toolpath is identical wherever the pool put it and
    whichever way it is turned. Stroke ORDER is intentionally ignored: the
    shared pool routes rapid travel through the other job's ink, and a
    plate-wide rapid that spans two jobs belongs to neither - the contract
    under test is that each job's CUTTING geometry is untouched by sharing a
    plate.
    """
    parser = PLTParser()
    signature: Counter[Tuple[int, int]] = Counter()
    for path in paths:
        document = parser.parse_string(path.read_text(encoding="ascii"))
        for stroke in document.stroke_paths:
            for points in _cutting_points(stroke):
                anchor_x, anchor_y = points[0]
                signature[(points[1][0] - anchor_x, points[1][1] - anchor_y)] += 1
    return _normalized(signature)


class TestSharedPlateSegmentation:
    """One shared sheet yields ONE toolpath per cutter, carrying every job."""

    @pytest.fixture()
    def batch(self, tmp_path: Path) -> Tuple[JobSetPlan, PerCutterExport]:
        plan = _plan("jobset_export_batch.yaml")
        return plan, _export_batch(plan, tmp_path)

    def test_both_jobs_pack_on_the_first_plate(
        self, batch: Tuple[JobSetPlan, PerCutterExport]
    ) -> None:
        """The merged pool is real: one plate carries both members."""
        plan, result = batch

        assert plan.jobset_id == "Export_Batch"
        assert [context.job_key for context in plan.contexts] == [ALPHA, BETA]
        assert result.plate_jobs == {1: (ALPHA, BETA)}

    def test_a_layer_two_jobs_share_is_named_for_the_batch(
        self, batch: Tuple[JobSetPlan, PerCutterExport]
    ) -> None:
        """The cutter both jobs use ships as ONE file, named for the batch.

        One run per tool is the point of the jobset, so a shared toolpath
        cannot name a single owner: it names the batch, and ownership is
        recorded per file. A layer only one job uses keeps that job's token,
        which is where its ink stays visible in the file list.
        """
        plan, result = batch

        assert sorted(path.name for path in result.plt_paths) == [
            "15_bh_Export_Batch.plt",
            "35_b_txt_Export_Beta.plt",
            "40_r_txt_Export_Alpha.plt",
        ]

    def test_file_jobs_lists_every_owner_of_every_file(
        self, batch: Tuple[JobSetPlan, PerCutterExport]
    ) -> None:
        """The map is the record of who is in each file: one job for a solo
        layer, both for the merged one (which names the batch, not a job)."""
        plan, result = batch

        owners = {result.file_jobs[path] for path in result.plt_paths}
        assert owners == {(ALPHA,), (ALPHA, BETA), (BETA,)}
        for path in result.plt_paths:
            tokens = path.name.count(ALPHA) + path.name.count(BETA)
            assert tokens == (0 if len(result.file_jobs[path]) > 1 else 1), path.name

    def test_text_layers_stay_split_by_cutter_and_color(
        self, batch: Tuple[JobSetPlan, PerCutterExport]
    ) -> None:
        """Segmentation is per job AND per (cutter, colour) layer: A cuts its
        labels with a 0.04 red tool, B with 0.035 blue, so the shared sheet
        yields one file per (job, layer) - never one merged file."""
        plan, result = batch

        text = sorted(p.name for p in result.plt_paths if "_txt_" in p.name)
        assert text == ["35_b_txt_Export_Beta.plt", "40_r_txt_Export_Alpha.plt"], text
        assert all(plan.jobset_id not in name for name in text)

    def test_borders_and_holes_merge_into_one_file_per_cutter(
        self, batch: Tuple[JobSetPlan, PerCutterExport]
    ) -> None:
        """Both jobs' borders and holes run together in a single BH file."""
        plan, result = batch

        structural = sorted(p.name for p in result.plt_paths if "_bh_" in p.name)
        assert structural == ["15_bh_Export_Batch.plt"], structural
        merged = next(p for p in result.plt_paths if "_bh_" in p.name)
        assert result.file_jobs[merged] == (ALPHA, BETA)

    def test_unused_declared_plate_writes_nothing(
        self, batch: Tuple[JobSetPlan, PerCutterExport]
    ) -> None:
        """The pool had two identical 24x16 plates and filled the first: the
        empty sheet produces no files and no owner record."""
        plan, result = batch

        assert 2 not in result.plate_jobs
        assert not [path for path in result.plt_paths if path.name.startswith("p02_")]

    def test_combined_preview_is_the_whole_plate(
        self, batch: Tuple[JobSetPlan, PerCutterExport]
    ) -> None:
        """The plate-level artifact (in-memory combined assembly, which the
        per-plate preview draws) keeps every job's ink, unlike the per-cutter
        files."""
        plan, result = batch

        combined = result.combined_by_plate[1]
        assert "SP1" in combined
        assert combined.count("IN;") == 1


class TestSegmentEquivalence:
    """Sharing a sheet moves ink between files; it never adds or drops any."""

    def test_job_geometry_is_unchanged_by_sharing_a_plate(self, tmp_path: Path) -> None:
        plan = _plan("jobset_export_batch.yaml")
        batch = _export_batch(plan, tmp_path / "batch")

        for context in plan.contexts:
            alone = _export_alone(context, tmp_path / f"alone-{context.job_key}")
            mine = [path for path, owners in batch.file_jobs.items() if context.job_key in owners]
            assert mine, f"no file was written for {context.job_key}"
            # A shared toolpath carries both jobs, so the invariant is
            # containment: everything a job engraves on its own is still
            # engraved when it shares the sheet.
            assert _segments(mine) >= _segments(alone.plt_paths), context.job_key

    def test_shared_plate_holds_more_ink_than_either_segment(self, tmp_path: Path) -> None:
        """Sanity check on the previous test: the combined plate really is the
        union, so 'equal geometry' is not 'equal file'."""
        plan = _plan("jobset_export_batch.yaml")
        batch = _export_batch(plan, tmp_path / "batch")

        shared = _segments(list(batch.file_jobs))
        alpha_only = _segments([p for p, owners in batch.file_jobs.items() if owners == (ALPHA,)])
        beta_only = _segments([p for p, owners in batch.file_jobs.items() if owners == (BETA,)])
        merged = _segments([p for p, owners in batch.file_jobs.items() if len(owners) > 1])
        assert merged, "the cutter both jobs use must ship as one shared file"
        # Every file falls in exactly one ownership class, and the three
        # classes are the whole sheet: the shared run is a union, not a copy.
        assert alpha_only + beta_only + merged == shared
        assert alpha_only != beta_only
        assert sum(alpha_only.values()) < sum(shared.values())


class TestPerSegmentHeaders:
    """A shared file's header block belongs to the job that owns the strokes."""

    def _velocity_line(self, path: Path) -> Optional[str]:
        """The ``VS`` header line of one exported file, if it has one."""
        for line in path.read_text(encoding="ascii").splitlines():
            if line.startswith("VS"):
                return line
        return None

    def test_each_segment_carries_its_own_tool_options(self, tmp_path: Path) -> None:
        """Two members of one batch declare different cutting speeds; the
        shipped ``job-config.json`` supplies the registry, so each segment's
        file must carry its own job's VS command - never the other's."""
        config = load_job_config(REPO_CONFIG)
        assert config is not None
        result = _export_batch(
            build_jobset_plan(FIXTURES / "jobset_tool_batch.yaml", job_config=config),
            tmp_path,
            job_config=config,
        )

        by_owner: Dict[str, List[str]] = {}
        for path, owners in result.file_jobs.items():
            line = self._velocity_line(path)
            assert line is not None, f"{path.name} carries no VS header"
            for owner in owners:
                by_owner.setdefault(owner, []).append(line)

        assert set(by_owner) == {"Tool_Alpha", "Tool_Beta"}
        alpha_lines = set(by_owner["Tool_Alpha"])
        beta_lines = set(by_owner["Tool_Beta"])
        assert len(alpha_lines) == 1 and len(beta_lines) == 1
        assert alpha_lines != beta_lines
        # 0.5 in/s vs 2.5 in/s, as the tool-option emitter formats them.
        assert alpha_lines.pop().startswith("VS0.50")
        assert beta_lines.pop().startswith("VS2.50")


class TestToolOptionDivergence:
    """A shared tool splits into one file per job ONLY where tool_options differ.

    One plate layer is one PLT file, and one PLT file carries one tool-option
    header block, so jobs that agree on ``tool_options`` merge into a single
    run and jobs that disagree cannot. The disagreement is a real cost (the
    cutter is physically re-run), so the writer warns about it. Both halves of
    that contract are pinned here: the split plus its warning, and the merge
    with no warning.
    """

    def test_divergent_tool_options_split_the_layer_and_warn(self, tmp_path: Path) -> None:
        """Alpha cuts at 0.5 in/s and Beta at 2.5 in/s on one shared sheet.

        They share the 15 (bh) and 75 (txt) cutters, so a merged run is
        impossible: each layer ships one file per job, named for the job whose
        headers it carries, and every split tool is reported once.
        """
        config = load_job_config(REPO_CONFIG)
        assert config is not None
        capture = _CaptureLogger()
        plan = build_jobset_plan(FIXTURES / "jobset_tool_batch.yaml", job_config=config)
        result = _export_batch(plan, tmp_path, job_config=config, logger=capture)

        # Set semantics: today `plt_paths` carries each text name TWICE - the
        # batch declares two identical sheets, and the second (empty) sheet
        # re-emits the first sheet's per-owner text files with byte-identical
        # content. One file on disk per layer per job is the contract; the
        # duplicate list entry is tracked separately.
        names = {path.name for path in result.plt_paths}
        assert names == {
            "15_bh_Tool_Alpha.plt",
            "15_bh_Tool_Beta.plt",
            "75_txt_Tool_Alpha.plt",
            "75_txt_Tool_Beta.plt",
        }, names
        # Each file belongs to exactly one job, and no file is batch-named.
        assert all(len(owners) == 1 for owners in result.file_jobs.values())
        assert all(plan.jobset_id not in path.name for path in result.plt_paths)

        assert len(capture.warnings) == 2, capture.warnings
        for warning in capture.warnings:
            assert "is split into 2 files because these jobs' tool_options headers" in warning
            assert "so the cutter is re-run (Tool_Alpha; Tool_Beta)" in warning
        assert any("the bh tool 15 " in w for w in capture.warnings), capture.warnings
        assert any("the txt tool 75 " in w for w in capture.warnings), capture.warnings

    def test_identical_tool_options_merge_the_layer_without_warning(self, tmp_path: Path) -> None:
        """The exclusivity half: members that agree on tool_options share one
        run, so a merged plate must NOT warn. Layers split for any other reason
        (here the 0.04 red / 0.035 blue text cutters of the two members) are
        not a tool_options split and are reported the same way: silently.
        """
        capture = _CaptureLogger()
        plan = _plan("jobset_export_batch.yaml")
        result = _export_batch(plan, tmp_path, logger=capture)

        assert capture.warnings == []
        merged = [path for path in result.plt_paths if "_bh_" in path.name]
        assert [path.name for path in merged] == ["15_bh_Export_Batch.plt"]
        assert result.file_jobs[merged[0]] == (ALPHA, BETA)


class _CaptureLogger:
    """Minimal :class:`TextLogger` stand-in that records the warnings it is sent.

    The export path warns only through an injected logger (``logger=None``
    silences it), so the split-warning contract is unreachable without one.
    """

    def __init__(self) -> None:
        self.warnings: List[str] = []
        self.infos: List[str] = []

    def warning(self, message: str, *args: object) -> None:
        self.warnings.append(message)

    def info(self, message: str, *args: object) -> None:
        self.infos.append(message)


class TestSingleJobPathIsInert:
    """Without provenance the export is exactly the pre-jobset pipeline."""

    def test_ownership_maps_stay_empty(self, tmp_path: Path) -> None:
        plan = _plan("jobset_export_batch.yaml")
        context = plan.contexts[0]

        result = _export_alone(context, tmp_path)

        assert result.file_jobs == {}
        assert result.plate_jobs == {}
        assert result.plt_paths

    def test_files_keep_the_job_id_token(self, tmp_path: Path) -> None:
        plan = _plan("jobset_export_batch.yaml")

        result = _export_alone(plan.contexts[1], tmp_path)

        assert all(BETA in path.name for path in result.plt_paths)
        assert ALPHA not in "".join(path.name for path in result.plt_paths)


def _pinned_pair(tmp_path: Path) -> JobSetPlan:
    """A two-member batch whose members each reserve their own sheet.

    ``jobset_a.yaml`` declares ``plate1`` and pins ``alpha-pinned`` to it and
    ``jobset_b.yaml`` pins ``beta-pinned`` to its own ``plate1``, so the merge
    namespaces the pins to ``{job_key}::plate1`` - each member keeps its own
    reserved sheet. Their unpinned labels need an UNPINNED pool plate: the
    layout engine treats a pinned plate as exclusive and removes it from the
    shared pool, so a batch that pins every declared sheet has nowhere left
    for unpinned work. The third member contributes that sheet.
    """
    for name in ("jobset_a.yaml", "jobset_b.yaml"):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        # The pins reserve material 'kw' sheets; the members' unpinned labels
        # need the same material to share a pool plate (materials never share
        # a plate), so the batch members declare it at job level.
        text = text.replace("  layout: columns\n", "  layout: columns\n  material: kw\n", 1)
        (tmp_path / name.replace("jobset_", "jobset_pin_")).write_text(text, encoding="utf-8")
    (tmp_path / "jobset_pin_c.yaml").write_text(
        "job:\n"
        "  job_name: Pool Gamma\n"
        "  layout: columns\n"
        "  material: kw\n"
        "  plates:\n"
        "    - id: plate3\n"
        "      width: 24.0\n"
        "      height: 16.0\n"
        "      material: kw\n"
        "  labels:\n"
        "    - id: pool-label\n"
        "      width: 4.0\n"
        "      height: 1.5\n"
        "      content:\n"
        "        - text: POOL LABEL\n"
        "          text_height: 0.5\n",
        encoding="utf-8",
    )
    (tmp_path / "jobset_pin_batch.yaml").write_text(
        "jobset:\n  name: Pin Batch\n  jobs:\n    - job: jobset_pin_a.yaml\n"
        "    - job: jobset_pin_b.yaml\n    - job: jobset_pin_c.yaml\n",
        encoding="utf-8",
    )
    return build_jobset_plan(tmp_path / "jobset_pin_batch.yaml", job_config=None)


class TestJobsetPinning:
    """A member's `plate_id` pin travels with the label into the shared pool.

    Labels pin themselves to a plate by PLATE ID (`LabelSpec.plate_id` ->
    `ResolvedLabel.plate_id`, read by the layout engine's pin pass), and
    `PlateSpec` has no pinning field, so a batch's pinning contract is
    entirely about ids naming plates that exist in the pool.
    """

    def test_pins_survive_the_merge_and_pack(self, tmp_path: Path) -> None:
        """Both members pin one label to their own `plate1`; the namespaced
        pins name distinct pool plates, so packing succeeds and each pinned
        label stays on the sheet its own job declared."""
        plan = _pinned_pair(tmp_path)

        assert [label.plate_id for label in plan.merged_labels if label.plate_id] == [
            "Jobset_Alpha::plate1",
            "Jobset_Beta::plate1",
        ]

        result = _export_batch(plan, tmp_path / "out")

        by_id = {label.id: label for label in plan.merged_labels}
        for pinned_id, expected in (
            ("Jobset_Alpha::alpha-pinned", "Jobset_Alpha::plate1"),
            ("Jobset_Beta::beta-pinned", "Jobset_Beta::plate1"),
        ):
            assert pinned_id in by_id
            home = [
                plate.plate_id
                for plate in result.packed_plates
                if any(pl.source_label.id == pinned_id for pl in plate.labels)
            ]
            assert home == [expected], home
        # The unpinned work shared the batch's one unreserved sheet.
        for loose_id in ("Jobset_Alpha::alpha-main", "Jobset_Beta::alpha-main"):
            home = [
                plate.plate_id
                for plate in result.packed_plates
                if any(pl.source_label.id == loose_id for pl in plate.labels)
            ]
            assert home == ["Pool_Gamma::plate3"], home

    def test_a_pin_naming_no_pool_plate_is_dropped(self, tmp_path: Path) -> None:
        """A pin the pool cannot honour is cleared with a warning instead of
        silently reserving a sheet that never receives its label."""
        plan = _pinned_pair(tmp_path)
        labels = [
            replace(label, plate_id="Nobody::plate1")
            if label.id.endswith("alpha-pinned")
            else label
            for label in plan.merged_labels
        ]

        plates = normalize_pinning(plan.merged_plates, labels)

        assert plates is not None and [plate.id for plate in plates] == [
            plate.id for plate in plan.merged_plates or []
        ]
        assert [label.plate_id for label in labels if label.id.endswith("alpha-pinned")] == [None]

    def test_an_unbounded_pool_clears_every_pin(self, tmp_path: Path) -> None:
        """With no declared plates there is no plate to name: the pins are
        dropped (and warned about) rather than left to the layout engine's
        silent "unresolvable id means unpinned" behaviour."""
        plan = _pinned_pair(tmp_path)
        labels = list(plan.merged_labels)

        assert normalize_pinning(None, labels) is None
        assert [label.plate_id for label in labels] == [None] * len(labels)
