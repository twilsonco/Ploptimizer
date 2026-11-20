"""Per-job accounting for a jobset run: usage, reports and the batch manifest.

A jobset packs every member onto shared plates and merges their toolpaths, so
the single batch report describing the run cannot answer the shop's per-job
questions ("what did THIS label sheet cost?"). Layer 5 of the jobset plan
solves that without inventing a second accounting engine: each member's numbers
are measured from **the files that carry its strokes** (the per-job segment
files the export stage writes), attributed inside those files by the same
slot-containment rule a single-job run uses. Subsetting is done with composite
ids, so no id is ever rewritten and a member's numbers are the numbers its own
toolpath made.

The batch keeps its own report (written by the CLI over the merged export, with
``usage.by_job`` added) plus :data:`MANIFEST_NAME`, the machine-readable index
that ties the batch's plates, files and per-job reports together.

Cross-job merging makes one number intentionally un-reconcilable: a merged
``txt`` stroke that spans two members' text is charged to **both** members'
labels, so the per-label breakdown exceeds the measured total by exactly the
shared-toolpath saving. That delta is reported as
``cross_job.distance_saved``; the per-job ``by_job`` breakdown (one charge per
stroke) stays exact.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from plt_optimizer.generate.generation_report import build_report, write_report
from plt_optimizer.generate.jobset import (
    JobProvenance,
    JobSetPlan,
    ResolvedJobContext,
)
from plt_optimizer.generate.usage_stats import (
    KIND_TEXT,
    PLATE_FRAME_LABEL,
    PLT_TYPE_BY_KIND,
    build_usage_stats,
)
from plt_optimizer.generate.vectorize import (
    PerCutterExport,
    _cutter_token_to_inches,
    _parse_plt_stem,
)

logger = logging.getLogger(__name__)

#: Name of the batch manifest written beside the jobset-level report.
MANIFEST_NAME: str = "jobset.json"

#: Row key used when a placement names no member (defensive: the export stamps
#: every jobset file with its owner).
UNATTRIBUTED_JOB: str = "unattributed"

#: Rounding of the manifest's money numbers (inches / seconds).
_MANIFEST_DECIMALS: int = 4


@dataclass(frozen=True)
class JobsetReports:
    """What a jobset report pass wrote.

    Attributes:
        job_reports: ``job_key`` -> that member's ``report.json`` path.
        usage_by_job: ``job_key`` -> that member's ``usage`` block (the same
            mapping its report carries, for callers that want the numbers).
        manifest_path: Where :func:`write_manifest` writes ``jobset.json``.
    """

    job_reports: Dict[str, Path]
    usage_by_job: Dict[str, Dict[str, Any]]
    manifest_path: Path


# ---------------------------------------------------------------------------
# Subsetting a merged export down to one member
# ---------------------------------------------------------------------------


def job_of_placement(export: PerCutterExport, plan: JobSetPlan) -> Dict[str, str]:
    """Map every packed placement to the member that owns it.

    Args:
        export: The merged export (its ``packed_plates`` carry the placements).
        plan: The jobset plan (its provenance names the owners).

    Returns:
        Placement id -> ``job_key``, the ``job_of_label`` map
        :func:`~plt_optimizer.generate.usage_stats.build_usage_stats` needs to
        emit ``usage.by_job``. Empty for a single-job plan. A sheet's own
        furniture (:data:`~plt_optimizer.generate.usage_stats.PLATE_FRAME_LABEL`
        - its border and registration marks) is charged to the first member of
        the first sheet carrying it, so that travel is counted exactly once and
        the members' rows add up to the batch's.
    """
    provenance = plan.provenance
    mapping: Dict[str, str] = {}
    for plate in export.packed_plates:
        for placement in plate.labels:
            source = getattr(placement, "source_label", None)
            source_id = str(getattr(source, "id", "") or "")
            owner = provenance.job_of_label(source_id)
            if owner is not None:
                mapping[str(placement.label_id)] = owner
    for owners in export.plate_jobs.values():
        if owners:
            mapping[PLATE_FRAME_LABEL] = owners[0]
            break
    return mapping


def sheet_first_owners(export: PerCutterExport) -> Dict[int, str]:
    """Map each plate to the first member whose labels sit on it.

    A sheet's own furniture (its border and registration marks) belongs to no
    label, so every report view charges it to the sheet's first member. Keying
    the rule by PLATE is what makes the views agree: a global
    "first member of the first sheet" entry gives the first member every
    sheet's frame, so its report over-counts and the members no longer sum to
    the batch.

    Args:
        export: The merged export (its ``plate_jobs`` names each sheet's owners
            in write order).

    Returns:
        1-based plate number -> that sheet's first ``job_key``; empty when the
        run recorded no per-plate owners (a single-job run).
    """
    return {int(number): tuple(owners)[0] for number, owners in export.plate_jobs.items() if owners}


def _plate_numbers(export: PerCutterExport) -> List[int]:
    """Return the plate numbers the run produced, ascending."""
    numbers = sorted(export.combined_by_plate)
    if numbers:
        return numbers
    return list(range(1, len(export.packed_plates) + 1))


def _file_plate(path: Path, numbers: Sequence[int]) -> Optional[int]:
    """Return the 1-based plate number a written file belongs to.

    Args:
        path: The written PLT/PDF path.
        numbers: The plate numbers the run produced (ascending).

    Returns:
        The plate number, or ``None`` when the name carries no plate token and
        the run has more than one plate (a file that cannot be placed).
    """
    token = _parse_plt_stem(path.stem).plate
    if token is not None:
        return int(token) if token.isdigit() else None
    return numbers[0] if len(numbers) == 1 else None


def _job_files(export: PerCutterExport, job_key: str) -> List[Path]:
    """Return the PLT files carrying one member's strokes.

    The export records every job whose strokes are in each written file (a
    plate layer is one toolpath shared by the jobs that use that tool), so this
    is a lookup, not an inference from names. A merged file is listed for EACH
    of its jobs - it is the batch's toolpath, and a member's own share of it is
    measured stroke by stroke (see :func:`build_job_usage`).

    Args:
        export: The merged export.
        job_key: The member being subset.

    Returns:
        The PLT paths carrying that member's strokes, in write order.
    """
    return [path for path in export.plt_paths if job_key in export.file_jobs.get(path, ())]


def _job_previews(
    export: PerCutterExport, plt_files: Sequence[Path]
) -> Tuple[List[Path], List[Path]]:
    """Return the member's simple previews and colour-coded default plots.

    The writer names every preview after the PLT it renders (same stem, ``.pdf``
    suffix; the colour-coded plots add ``_default``), so a member's previews are
    exactly the mirrors of the files that carry its strokes - no second naming
    rule to drift from.

    Args:
        export: The merged export.
        plt_files: The member's PLT files.

    Returns:
        ``(pdf_paths, default_pdf_paths)`` for that member.
    """
    stems = {path.stem for path in plt_files}
    pdfs = [path for path in export.pdf_paths if path.stem in stems]
    defaults = [path for path in export.default_pdf_paths if path.stem in stems]
    return pdfs, defaults


def _file_jobs(export: PerCutterExport, path: Path) -> List[str]:
    """Return the jobs one file's toolpath carries (``[]`` for a solo run)."""
    return list(export.file_jobs.get(path, ()))


def _job_plates(plan: JobSetPlan, export: PerCutterExport, job_key: str) -> List[Any]:
    """Return the batch's plates carrying only this member's placements.

    Plate numbers and geometry stay global, so a member's report names the same
    sheets the machine files do; only the placement list is filtered.

    Args:
        plan: The jobset plan (owner of the provenance table).
        export: The merged export.
        job_key: The member being subset.

    Returns:
        One entry per packed plate (a :class:`PackedPlate` copy).
    """
    provenance = plan.provenance
    plates: List[Any] = []
    for plate in export.packed_plates:
        mine = []
        for placement in plate.labels:
            source = getattr(placement, "source_label", None)
            source_id = str(getattr(source, "id", "") or "")
            owner = provenance.job_of_label(source_id) if provenance else None
            if owner == job_key:
                mine.append(placement)
        plates.append(replace(plate, labels=mine))
    return plates


def _job_combined(
    export: PerCutterExport, plt_files: Sequence[Path], numbers: Sequence[int]
) -> Dict[int, str]:
    """Concatenate one member's own files into its per-plate content.

    Args:
        export: The merged export (unused except for symmetry with the callers).
        plt_files: The member's PLT files.
        numbers: The plate numbers the run produced.

    Returns:
        Plate number -> that member's PLT text on it (the ``combined_by_plate``
        a member's usage measurement needs, so a stroke is only ever matched
        against that member's slots).
    """
    del export
    combined: Dict[int, str] = {}
    for path in plt_files:
        number = _file_plate(path, numbers)
        if number is None:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover - defensive, files were just written
            logger.warning("Jobset report: cannot read %s (%s)", path, exc)
            continue
        combined[number] = f"{combined.get(number, '')}{text}"
    return combined


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


def _batch_job_of_label(plan: JobSetPlan, export: PerCutterExport) -> Dict[str, str]:
    """Map every placement id of the shared run to its owning member.

    A member's report is measured from the batch's MERGED files, whose strokes
    belong to several jobs, so attribution needs the whole pool's owner map: a
    member's own slots cannot explain a neighbour's strokes, and an owner the
    map does not carry is counted as unattributed. Sheet furniture (a plate's
    border / registration marks) is charged to the first member of the first
    sheet that carries it, so the furniture is counted exactly once and the
    members' rows add up to the batch's.

    Args:
        plan: The jobset plan (owner of the provenance table).
        export: The merged export (packed plates and per-plate owners).

    Returns:
        ``label_id -> job_key`` for every placement of the pool, plus the
        plate-frame entry when the run has one.
    """
    provenance = plan.provenance
    mapping: Dict[str, str] = {}
    for plate in export.packed_plates:
        for placement in plate.labels:
            source_id = str(getattr(getattr(placement, "source_label", None), "id", "") or "")
            owner = provenance.job_of_label(source_id) if provenance else None
            if source_id and owner:
                mapping[source_id] = owner
    for owners in export.plate_jobs.values():
        if owners:
            mapping[PLATE_FRAME_LABEL] = owners[0]
            break
    return mapping


def build_job_usage(
    plan: JobSetPlan,
    export: PerCutterExport,
    context: ResolvedJobContext,
    *,
    job_config: Any = None,
) -> Dict[str, Any]:
    """Measure one member's production usage out of the shared run.

    The counts are the member's own (its labels, its placements, its sheets); the
    tool travel is measured from the batch's files - the merged toolpaths
    included - resolved against the BATCH's label/plate pool (so every stroke in a
    merged file finds its slot) and restricted stroke by stroke to the strokes
    this member owns (``only_job``). A member's report therefore says what the
    machine engraved FOR it, its by_label rows partition the batch's, and the
    members add up to the batch view without double-counting a shared run.

    Args:
        plan: The jobset plan.
        export: The merged export (segment files, plates, placements).
        context: The member's resolved context.
        job_config: The run's job config (the cutter-rate registry).

    Returns:
        That member's ``usage`` block.
    """
    numbers = _plate_numbers(export)
    plt_files = _job_files(export, context.job_key)
    return build_usage_stats(
        resolved_labels=list(context.resolved_labels),
        packed_plates=_job_plates(plan, export, context.job_key),
        combined_by_plate=_job_combined(export, plt_files, numbers),
        plt_files=plt_files,
        material_by_plate=dict(export.material_by_plate),
        job_spec=context.spec,
        job_config=job_config,
        job_of_label=job_of_placement(export, plan),
        plate_owner_of=sheet_first_owners(export),
        file_jobs=dict(export.file_jobs),
        attribution_labels=list(plan.merged_labels),
        attribution_plates=list(export.packed_plates),
        only_job=context.job_key,
    )


def cross_job_summary(
    root_usage: Optional[Mapping[str, Any]], export: PerCutterExport
) -> Dict[str, Any]:
    """Quantify what sharing toolpaths saved across the batch.

    ``distance_saved`` is the excess of the per-label sum over the measured
    total: a stroke a merge handed to two labels is counted in each, and the
    excess is travel the batch does not make. ``strokes_merged`` is the number
    of strokes in shared (``txt``) layers on the mixed plates - the merge
    candidates, an upper bound of the strokes that literally span jobs.

    Args:
        root_usage: The batch's ``usage`` block (``None`` when it has none).
        export: The merged export (plate ownership + per-file stroke rows).

    Returns:
        The manifest's ``cross_job`` mapping.
    """
    distance = dict(root_usage or {}).get("distance") or {}
    rows = list(distance.get("by_label") or [])
    by_label = sum(
        float(row.get("cutting") or 0.0) + float(row.get("rapid") or 0.0) for row in rows
    )
    total = float((distance.get("total") or {}).get("total") or 0.0)
    mixed = {number for number, owners in export.plate_jobs.items() if len(tuple(owners)) > 1}
    strokes = 0
    for row in distance.get("files") or []:
        if row.get("kind") == KIND_TEXT and row.get("plate") in mixed:
            strokes += int(row.get("strokes") or 0)
    return {
        "strokes_merged": strokes,
        "distance_saved": round(max(by_label - total, 0.0), _MANIFEST_DECIMALS),
        "note": (
            "per-job by_label sums exceed by_job totals; the delta is the shared-toolpath saving"
        ),
    }


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _file_meta(path: Path, output_dir: Path, export: PerCutterExport) -> Dict[str, Any]:
    """Return the manifest row describing one written PLT file.

    Args:
        path: The written PLT path.
        output_dir: The run's output directory (paths are stored relative to
            it, in forward-slash form, so the manifest moves with the folder).
        export: The merged export (file ownership).

    Returns:
        ``path``/``plate``/``layer``/``cutter``/``color``/``jobs``/``bytes``/
        ``sha256``.
    """
    stem = _parse_plt_stem(path.stem)
    plate: Optional[int] = None
    if stem.plate is not None and stem.plate.isdigit():
        plate = int(stem.plate)
    cutter: Optional[float] = None
    if stem.cutter is not None:
        cutter = round(_cutter_token_to_inches(stem.cutter), _MANIFEST_DECIMALS)
    payload = path.read_bytes()
    return {
        "path": _relative(path, output_dir),
        "plate": plate,
        "layer": PLT_TYPE_BY_KIND.get(stem.kind or "", stem.kind),
        "cutter": cutter,
        "color": stem.color,
        "jobs": _file_jobs(export, path),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _job_order(plan: JobSetPlan, job_key: str) -> int:
    """Return a member's position in jobset-file order (last when unknown)."""
    for index, context in enumerate(plan.contexts):
        if context.job_key == job_key:
            return index
    return len(plan.contexts)


def _relative(path: Optional[Path], root: Path) -> Optional[str]:
    """Return ``path`` relative to ``root`` in forward-slash form."""
    if path is None:
        return None
    resolved = Path(path)
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def _job_usage_totals(usage: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Pull the numbers a manifest job row carries out of a usage block."""
    block = dict(usage or {})
    distance = block.get("distance") or {}
    time_block = block.get("time") or {}
    weight_block = block.get("weight") or {}
    unattributed = distance.get("unattributed") or {}
    time_total = (time_block.get("total") or {}).get("total")
    weight_total = (weight_block.get("total") or {}).get("lb")
    return {
        "distance_total": (distance.get("total") or {}).get("total"),
        "time_total": time_total if time_block.get("available") else None,
        # Pounds, estimated from the labels' nominal area; ``None`` when the
        # member configures no material_weight_per_square_inch (the same
        # "not estimated is not zero" rule as ``time_total``).
        "weight_total": weight_total if weight_block.get("available") else None,
        "unattributed": round(
            float(unattributed.get("cutting") or 0.0) + float(unattributed.get("rapid") or 0.0),
            _MANIFEST_DECIMALS,
        ),
    }


def build_manifest(
    plan: JobSetPlan,
    export: PerCutterExport,
    *,
    output_dir: Path,
    usage_by_job: Mapping[str, Mapping[str, Any]],
    reports: Mapping[str, Path],
    root_usage: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the ``jobset.json`` document.

    Args:
        plan: The jobset plan.
        export: The merged export the batch wrote.
        output_dir: The run's output directory (manifest paths are relative to
            it).
        usage_by_job: ``job_key`` -> that member's ``usage`` block.
        reports: ``job_key`` -> that member's ``report.json`` path.
        root_usage: The batch's ``usage`` block, for the cross-job numbers.

    Returns:
        The manifest, JSON-ready.
    """
    numbers = _plate_numbers(export)
    provenance: Optional[JobProvenance] = plan.provenance

    jobs = [
        {
            "job_key": context.record.key,
            "job_id": context.record.job_id,
            "job_name": context.record.job_name,
            "source_file": context.record.spec_path.name,
            "spec": _relative(context.record.spec_path, output_dir),
            "labels": len(context.record.label_ids),
            "plates_declared": list(context.record.plate_ids),
            "report": _relative(reports.get(context.record.key), output_dir),
            "usage": _job_usage_totals(usage_by_job.get(context.record.key)),
        }
        for context in plan.contexts
    ]

    plates: List[Dict[str, Any]] = []
    for index, plate in enumerate(export.packed_plates, start=1):
        labels: Dict[str, List[str]] = {}
        for placement in plate.labels:
            source_id = str(getattr(placement.source_label, "id", ""))
            owner = provenance.job_of_label(source_id) if provenance else None
            key = owner or UNATTRIBUTED_JOB
            base = provenance.base_id(source_id) if provenance else source_id
            bucket = labels.setdefault(key, [])
            if base not in bucket:
                bucket.append(base)
        plates.append(
            {
                "plate": index,
                "id": plate.plate_id,
                "material": plate.material,
                "jobs": list(export.plate_jobs.get(index, ())),
                "labels": {
                    key: labels[key]
                    for key in sorted(labels, key=lambda key: _job_order(plan, key))
                },
                "files": [
                    _relative(path, output_dir)
                    for path in export.plt_paths
                    if _file_plate(path, numbers) == index
                ],
            }
        )

    return {
        "jobset": {
            "id": plan.jobset_id,
            "name": plan.jobset_name,
            "spec": plan.jobset_path.name,
            "layout": getattr(plan.layout, "value", str(plan.layout)),
            "job_count": len(plan.contexts),
            "succeeded": sum(1 for job in jobs if job["report"]),
            "status": "success" if all(job["report"] for job in jobs) else "partial",
            "total_labels": len(plan.merged_labels),
            "total_plates": len(plates),
            "total_files": len(export.plt_paths),
            # Batch weight total, pounds (null when no member configures a
            # material_weight_per_square_inch).
            "total_weight_lb": (((root_usage or {}).get("weight") or {}).get("total") or {}).get(
                "lb"
            )
            if ((root_usage or {}).get("weight") or {}).get("available")
            else None,
        },
        "gate": {"warnings": list(plan.warnings), "errors": []},
        "jobs": jobs,
        "plates": plates,
        "files": [_file_meta(path, output_dir, export) for path in export.plt_paths],
        "cross_job": cross_job_summary(root_usage, export),
    }


# ---------------------------------------------------------------------------
# The report pass
# ---------------------------------------------------------------------------


def manifest_path_for(output_dir: Path) -> Path:
    """Return the batch manifest path of a run's output directory."""
    return Path(output_dir) / "generation_report" / MANIFEST_NAME


def write_job_reports(
    plan: JobSetPlan,
    export: PerCutterExport,
    *,
    output_dir: Path,
    job_config: Any = None,
    started_at: str = "",
    finished_at: str = "",
    duration_seconds: float = 0.0,
    tools: Optional[Mapping[str, Any]] = None,
    options: Optional[Mapping[str, Any]] = None,
    timings: Any = None,
) -> JobsetReports:
    """Write every member's own report over the shared run.

    Each member gets ``OUT/<job_id>/generation_report/{report.json,summary.txt}``
    measured over its own segment files - the same report shape a standalone run
    writes, with the ids its own file declares.

    Args:
        plan: The jobset plan the export was made from.
        export: The merged export the batch wrote.
        output_dir: The run's output directory (``plt/``, ``pdf/``, reports).
        job_config: The run's job config (cutter rates for the time estimate).
        started_at: Run start timestamp (ISO 8601), shared by every report.
        finished_at: Run end timestamp (ISO 8601).
        duration_seconds: Wall-clock duration of the batch run.
        tools: The cutter inventory block (identical in every report).
        options: The CLI options block (identical in every report).
        timings: The stage-timing collector of the batch run.

    Returns:
        The :class:`JobsetReports` record of what was written.
    """
    reports: Dict[str, Path] = {}
    usage_by_job: Dict[str, Dict[str, Any]] = {}
    for context in plan.contexts:
        record = context.record
        plt_files = _job_files(export, record.key)
        pdfs, defaults = _job_previews(export, plt_files)
        usage = build_job_usage(plan, export, context, job_config=job_config)
        usage_by_job[record.key] = usage
        report_dir = output_dir / record.job_id / "generation_report"
        document = build_report(
            job_name=record.job_name,
            job_id=record.job_id,
            spec_path=record.spec_path,
            output_dir=output_dir,
            report_dir=report_dir,
            status="success",
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration_seconds,
            tools=dict(tools or {}),
            options=dict(options or {}),
            plate_count=context.plate_count,
            resolved_labels=list(context.resolved_labels),
            rendered_by_id=dict(export.rendered_labels),
            packed_plates=_job_plates(plan, export, record.key),
            plt_files=plt_files,
            pdf_files=pdfs,
            default_pdf_files=defaults,
            timings=timings,
            usage=usage,
            id_prefix=f"{record.key}::",
        )
        json_path, _summary = write_report(report_dir, document)
        reports[record.key] = json_path
        logger.debug("Jobset member report written: %s", json_path)

    return JobsetReports(
        job_reports=reports,
        usage_by_job=usage_by_job,
        manifest_path=manifest_path_for(output_dir),
    )


def build_jobset_block(
    plan: JobSetPlan,
    export: PerCutterExport,
    reports: JobsetReports,
    *,
    output_dir: Path,
    root_usage: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the batch report's ``jobset`` key (the per-job index).

    Args:
        plan: The jobset plan.
        export: The merged export the batch wrote.
        reports: What :func:`write_job_reports` wrote.
        output_dir: The run's output directory (report paths are stored
            relative to it).
        root_usage: The batch's ``usage`` block, for the cross-job numbers.

    Returns:
        ``{"id", "name", "spec", "jobs": [...], "cross_job": {...}}``.
    """
    return {
        "id": plan.jobset_id,
        "name": plan.jobset_name,
        "spec": plan.jobset_path.name,
        "jobs": [
            {
                "job_key": context.record.key,
                "job_id": context.record.job_id,
                "job_name": context.record.job_name,
                "report": _relative(reports.job_reports.get(context.record.key), output_dir),
                **_job_usage_totals(reports.usage_by_job.get(context.record.key)),
            }
            for context in plan.contexts
        ],
        "cross_job": cross_job_summary(root_usage, export),
    }


def write_manifest(
    plan: JobSetPlan,
    export: PerCutterExport,
    reports: JobsetReports,
    *,
    output_dir: Path,
    root_usage: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Write ``jobset.json`` and return the document that was written.

    Args:
        plan: The jobset plan.
        export: The merged export the batch wrote.
        reports: What :func:`write_job_reports` wrote.
        output_dir: The run's output directory.
        root_usage: The batch's ``usage`` block, for the cross-job numbers.

    Returns:
        The manifest document.
    """
    manifest = build_manifest(
        plan,
        export,
        output_dir=output_dir,
        usage_by_job=reports.usage_by_job,
        reports=reports.job_reports,
        root_usage=root_usage,
    )
    reports.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    reports.manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest
