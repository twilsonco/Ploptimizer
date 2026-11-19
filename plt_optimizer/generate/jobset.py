"""Jobset specs: pack many job files onto shared plates in one run.

A *jobset* is a thin composite YAML that lists existing per-job spec files::

    jobset:
      - job: seer-2963.yaml
      - job: coolsys-1369.yaml

Every member is resolved through exactly the single-job path
(:func:`plt_optimizer.generate.job_prep.prepare_job`), then all members'
labels are packed as **one pool** onto the union of their plates (plus
auto-allocated sheets). Packing is shared; emission is not merged: every
written PLT is **one job's toolpath** (a shared plate yields a ``txt`` file
per job/cutter/colour and one ``bh`` file per job carrying that job's borders
(SP2) and holes (SP3)), because a stroke routed across jobs would belong to
two jobs and stop being per-job reportable. Material utilisation is the point
- packing jobs independently leaves a gap at the end of each job's last
sheet.

Id namespacing
--------------
Label and plate ids are unique **per job**, so merging them is not: two jobs
can each own a ``plate1``, and every consumer keyed by id
(:func:`plt_optimizer.build_usage_stats`' ``by_label``, the layout report, the
relief side tables) silently collides. The merge therefore rewrites ids into
the composite form ``"{job_key}::{id}"``, where ``job_key`` is the sanitized
job name (ASCII ``[A-Za-z0-9_]`` by construction, so the first ``::`` splits
unambiguously). Provenance is a **side table** (:class:`JobProvenance`) keyed
by composite id, never a schema field: ``LabelSpec``/``PlateSpec`` sit on the
cascade/parity surface (``schema_parity.py``) and a jobset-only field there
would leak into every generated cascade document.

Composite ids never reach a filename. Machine-file stems carry the id of the
job whose strokes the file holds (every file is one job's toolpath), so a
batch's names match what each job produces standalone; only the plate-level
artifact - the combined ``*_all_*`` preview, which really does span the jobs -
keeps the jobset token (see
:func:`plt_optimizer.generate.vectorize._segment_job_id`).

Gate
----
Members are independent files, so conflicts surface only at packing time.
The gate (:func:`evaluate_jobset_gate`) splits them honestly: geometry that
the merge cannot honour is a hard :class:`JobSetError`; a conflict with a
deterministic, visible winner is a warning recorded in the jobset manifest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, List, Mapping, Optional, Sequence

import yaml

from plt_optimizer.core.filenames import sanitize_filename_component
from plt_optimizer.generate.job_prep import (
    JobPreparationError,
    PreparedJob,
    default_plate_clearance,
    prepare_job,
    sanitize_job_id,
)
from plt_optimizer.generate.resolution import ResolvedLabel
from plt_optimizer.generate.schema import (
    JobSetSpec,
    JobSpec,
    LayoutMode,
    PlateSpec,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from plt_optimizer.generate.job_config import JobConfig
    from plt_optimizer.utils.stage_timing import StageTimings

LOG = logging.getLogger(__name__)

# Separator of the composite id form ``"{job_key}::{id}"``.
ID_SEPARATOR = "::"

# Legend colours for the per-job overlay on a jobset plate proof, indexed by
# the job's position in the jobset file (cycled). Ordered so that neighbours
# stay distinguishable in greyscale proofs as well as in colour.
JOB_LEGEND_PALETTE: tuple[str, ...] = (
    "tab:blue",
    "tab:red",
    "tab:green",
    "tab:purple",
    "tab:brown",
    "tab:olive",
    "tab:cyan",
    "tab:pink",
    "tab:gray",
    "tab:orange",
)

# Dash patterns cycled after the palette is exhausted, so a 12-job batch is
# still readable: colour index ``i % 10`` pairs with style index ``i // 10``.
# Ten colours x these three patterns = 30 distinguishable (colour, style)
# pairs. Only matplotlib *named* patterns are listed: a tuple dash pattern
# (``(0, (5, 1, 1, 1))``) has no string form the API accepts, and a batch
# big enough to need a 31st pair is not a plate a person reads.
JOB_LEGEND_STYLES: tuple[str, ...] = ("--", "-.", ":")


class JobSetError(ValueError):
    """A jobset file is invalid, or its members break the jobset gate.

    Attributes:
        errors: The gate errors, in detection order (job-file order).
    """

    def __init__(self, errors: Sequence[str]) -> None:
        """Store the gate errors and render them as the message.

        Args:
            errors: Non-empty sequence of human-readable error lines.
        """
        self.errors = tuple(errors)
        super().__init__("Jobset gate failed:\n  - " + "\n  - ".join(self.errors))


@dataclass(frozen=True)
class JobRecord:
    """Identity of one jobset member, in jobset-file order.

    Attributes:
        key: Composite-id prefix (the sanitized ``job_name``, made unique).
        job_id: Filename token for this job (``key`` unless a collision
            appended a numeric suffix).
        job_name: The member's declared ``job_name``.
        spec_path: Absolute path of the member spec file.
        label_ids: The member's label ids as the member file declares them
            (un-prefixed; the suffixes a ``count``/replacement expansion
            added are included).
        plate_ids: The member's plate ids as declared (empty in unbounded
            mode).
    """

    key: str
    job_id: str
    job_name: str
    spec_path: Path
    label_ids: tuple[str, ...] = ()
    plate_ids: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        """Legend/title text: the declared name, falling back to the id."""
        return self.job_name or self.job_id


@dataclass(frozen=True)
class ResolvedJobContext:
    """One member job, prepared and with its ids namespaced.

    Attributes:
        record: Identity of the member.
        spec: The member's expanded :class:`JobSpec` (unchanged - ids are
            namespaced on the *resolved* labels and plates only, so the
            member's own report prints its own ids).
        prepared: The full :class:`PreparedJob` (the same object a
            single-job run hands to the export stage).
        resolved_labels: The member's resolved labels with composite ids.
        plates: The member's plates with composite ids (``None`` in
            unbounded mode; the packer then allocates shared sheets).
        plate_count: Number of declared plates (0 = unbounded).
        layout: The member's job-level layout mode.
        allow_rotation: The member's rotation permission.
        default_plate_clearance: The member's job-level ``(left, top)``
            plate clearance, or ``None``.
    """

    record: JobRecord
    spec: JobSpec
    prepared: PreparedJob
    resolved_labels: tuple[ResolvedLabel, ...]
    plates: Optional[tuple[PlateSpec, ...]]
    plate_count: int
    layout: LayoutMode
    allow_rotation: bool
    default_plate_clearance: Optional[tuple[float, float]]

    @property
    def job_key(self) -> str:
        """This member's composite-id prefix."""
        return self.record.key


@dataclass(frozen=True)
class JobProvenance:
    """Composite-id side table: who owns what. ``None`` means single job.

    Attributes:
        jobset_id: Key of the artifacts the set itself writes (the batch
            report, `jobset.json`, the combined plate proofs) and the plot
            titles. PLT files stay named for their owning job.
        jobs: Member records in jobset-file order.
        label_jobs: Composite label id -> owning ``job_key``.
        plate_jobs: Composite plate id -> declaring ``job_key``.
    """

    jobset_id: str
    jobs: tuple[JobRecord, ...]
    label_jobs: Mapping[str, str] = field(default_factory=dict)
    plate_jobs: Mapping[str, str] = field(default_factory=dict)

    @property
    def job_keys(self) -> tuple[str, ...]:
        """Owning job keys in jobset-file order."""
        return tuple(job.key for job in self.jobs)

    def record(self, job_key: str) -> Optional[JobRecord]:
        """Return the record for ``job_key`` (``None`` when unknown).

        Args:
            job_key: A composite-id prefix.

        Returns:
            The matching :class:`JobRecord`, or ``None``.
        """
        for job in self.jobs:
            if job.key == job_key:
                return job
        return None

    def job_of_label(self, label_id: str) -> Optional[str]:
        """Return the job key owning a composite label id.

        Args:
            label_id: Composite (or plain, for a single-job plate) label id.

        Returns:
            The owning ``job_key``, or ``None`` when the id is unknown.
        """
        owner = self.label_jobs.get(label_id)
        if owner is not None:
            return owner
        prefix, _ = split_composite_id(label_id)
        return prefix

    def job_of_plate(self, plate_id: str) -> Optional[str]:
        """Return the job key declaring a composite plate id.

        Args:
            plate_id: Composite (or auto-allocated) plate id.

        Returns:
            The declaring ``job_key``, or ``None`` for a shared
            ``default_plate_N`` sheet (no single owner).
        """
        owner = self.plate_jobs.get(plate_id)
        if owner is not None:
            return owner
        prefix, _ = split_composite_id(plate_id)
        return prefix

    def owners_of(self, label_ids: Iterable[str]) -> tuple[str, ...]:
        """Return the jobs present on a packed plate, in jobset-file order.

        Args:
            label_ids: The composite label ids packed onto the plate.

        Returns:
            The owning job keys, deduplicated and in jobset-file order.
        """
        present = {self.job_of_label(label_id) for label_id in label_ids}
        return tuple(key for key in self.job_keys if key in present)

    def is_mixed(self, label_ids: Iterable[str]) -> bool:
        """Return ``True`` when a plate carries labels from 2+ jobs.

        Args:
            label_ids: The composite label ids packed onto the plate.

        Returns:
            ``True`` when more than one job owns a label on the plate.
        """
        return len(self.owners_of(label_ids)) > 1

    def base_id(self, composite_id: str) -> str:
        """Return a composite id with its ``{job_key}::`` prefix removed.

        Args:
            composite_id: A namespaced label or plate id.

        Returns:
            The id as the owning member file declares it (a plain id is
            returned unchanged).
        """
        _, base = split_composite_id(composite_id)
        return base


@dataclass(frozen=True)
class JobSetPlan:
    """A validated, merged jobset: everything the export stage needs.

    Attributes:
        jobset_id: Filename token / report ``job_id`` for the batch.
        jobset_name: Human-readable batch name (report / plot titles).
        jobset_path: The jobset file the plan was built from.
        contexts: Member contexts in jobset-file order.
        provenance: The composite-id side table.
        merged_labels: Every member's namespaced labels, concatenated in
            jobset-file order (the packing pool).
        merged_plates: The union of the members' namespaced plates in
            jobset-file order, or ``None`` when no member declares plates
            (unbounded shared allocation).
        layout: The shared layout mode (gate-enforced identical).
        allow_rotation: The pool-wide rotation permission.
        default_plate_clearance: ``(left, top)`` clearance for auto-allocated
            sheets (the first member's value when they diverge).
        warnings: Gate warnings, in detection order.
    """

    jobset_id: str
    jobset_name: str
    jobset_path: Path
    contexts: tuple[ResolvedJobContext, ...]
    provenance: JobProvenance
    merged_labels: list[ResolvedLabel]
    merged_plates: Optional[list[PlateSpec]]
    layout: LayoutMode
    allow_rotation: bool
    default_plate_clearance: Optional[tuple[float, float]]
    warnings: tuple[str, ...] = ()

    @property
    def job_count(self) -> int:
        """Number of member jobs."""
        return len(self.contexts)


def normalize_pinning(
    merged_plates: Optional[list[PlateSpec]],
    merged_labels: list[ResolvedLabel],
) -> Optional[list[PlateSpec]]:
    """Keep plate pins that still describe the shared packing pool.

    A label pins itself to a plate by PLATE ID
    (``ResolvedLabel.plate_id``, read at
    :func:`plt_optimizer.generate.packing.layout`'s pin grouping), and that
    group enforces per-plate geometry (``left_clearance`` / ``top_clearance``
    / ``hole_margin`` / ``h_margin`` / ``v_margin``, applied per plate by
    ``_plate_clearances``). Jobset namespacing rewrites plate ids to
    ``{job_key}::{plate_id}`` and the matching ``plate_id`` on that job's
    labels, so pins keep pointing at their own sheet and a member's plate
    geometry stays attached to it - that part is honoured, and it composes
    with the shared pool: a plate that one label pins to is off-limits to the
    rest, so pinning is how a batch keeps a job's work on its declared sheet.

    What cannot survive is a pin to a plate that no longer exists. The layout
    engine treats an unresolvable ``plate_id`` as "not pinned"
    (``pinned.get(plate_id, [])`` - no error), so a member's pin to a plate
    that the pool does not carry would drop that label into the shared pool:
    the sheet it deliberately reserved for its own work then packs other
    jobs' labels, and its label lands wherever the pool puts it. A
    replacement-text label has no ``plate_id`` at all (expansion has no
    plate-level cascade), so a member that pins a label it generates from a
    ``replacement_text_file`` reserves its sheet for work the pool never
    names - which the layout engine then reports as "every declared plate is
    pinned, N unpinned labels remain", with no hint of the real cause. Name
    the pin, drop the ones the pool cannot honour, and say so.

    Args:
        merged_plates: The pool's plate list (``None`` = unbounded shared
            allocation; no plate exists to be named, so every pin is dropped).
        merged_labels: The pool's labels, updated in place: an unresolvable
            pin is cleared (``ResolvedLabel`` is frozen, so the entry is
            replaced), and the label falls back to normal packing.

    Returns:
        ``merged_plates``, unchanged when every pin resolves.
    """
    if merged_plates is None:
        pinned = [index for index, label in enumerate(merged_labels) if label.plate_id is not None]
        if pinned:
            LOG.warning(
                "Ignoring plate_id on %d label(s) (%s): the batch allocates shared plates, "
                "so no plate exists to pin to and the labels will pack anywhere in the pool.",
                len(pinned),
                ", ".join(merged_labels[index].id for index in pinned[:5]),
            )
            for index in pinned:  # ResolvedLabel is frozen; replace in the pool list.
                merged_labels[index] = replace(merged_labels[index], plate_id=None)
        return None

    known = {plate.id for plate in merged_plates}
    orphaned = [
        index
        for index, label in enumerate(merged_labels)
        if label.plate_id is not None and label.plate_id not in known
    ]
    if orphaned:
        LOG.warning(
            "Ignoring plate_id on %d label(s) (%s): no plate of that id is in the pool, "
            "so they will pack anywhere rather than silently land on a stranger's sheet.",
            len(orphaned),
            ", ".join(merged_labels[index].id for index in orphaned[:5]),
        )
        for index in orphaned:  # ResolvedLabel is frozen; replace in the pool list.
            merged_labels[index] = replace(merged_labels[index], plate_id=None)
    return merged_plates


def split_composite_id(composite_id: str) -> tuple[Optional[str], str]:
    """Split a composite id into ``(job_key, base_id)``.

    Args:
        composite_id: An id in ``"{job_key}::{base}"`` form, or a plain id.

    Returns:
        ``(job_key, base)`` for a composite id, ``(None, composite_id)`` for
        a plain one. The FIRST separator wins, so a base id that itself
        contains ``::`` survives a round trip.
    """
    prefix, sep, base = composite_id.partition(ID_SEPARATOR)
    if not sep:
        return None, composite_id
    return prefix, base


def make_composite_id(job_key: str, base_id: str) -> str:
    """Build the composite id of ``base_id`` under ``job_key``.

    Args:
        job_key: The owning member's composite-id prefix.
        base_id: The id as the member file declares it.

    Returns:
        ``"{job_key}::{base_id}"``.
    """
    return f"{job_key}{ID_SEPARATOR}{base_id}"


def jobset_id_of(spec: JobSetSpec, jobset_path: Path) -> str:
    """Return the jobset's filename/report token.

    Args:
        spec: The parsed jobset spec.
        jobset_path: The jobset file (its stem is the fallback name).

    Returns:
        A Windows-safe token from ``spec.name`` when set, else the file stem
        (falling back to ``"jobset"`` for a stem that sanitizes to nothing).
    """
    return sanitize_filename_component(spec.name or jobset_path.stem, fallback="jobset")


def legend_style_for(index: int) -> tuple[str, str]:
    """Return the deterministic ``(color, linestyle)`` of a legend entry.

    Args:
        index: The job's zero-based position in the jobset file.

    Returns:
        The palette colour and dash pattern for that position.
    """
    color = JOB_LEGEND_PALETTE[index % len(JOB_LEGEND_PALETTE)]
    style = JOB_LEGEND_STYLES[(index // len(JOB_LEGEND_PALETTE)) % len(JOB_LEGEND_STYLES)]
    return color, style


@dataclass(frozen=True)
class JobOverlayBox:
    """One label's provenance rectangle on a jobset plate proof (inches).

    One box is emitted per PLACEMENT: the rectangle of the slot that label
    fills drawn INBOARD of its border stroke (inset by the overlay margin), so
    the dashed key reads as part of the label rather than as a box around it in
    the scrap. EVERY label on a jobset proof carries one - a sheet one job
    occupies alone is outlined too - because a union box around a job's labels
    says nothing about which labels inside it are whose, and that is the
    question the proof has to answer. The legend lists one row per job (the
    plotter collapses repeated rows), naming each colour and dash pattern.

    Attributes:
        x: Left edge in inches (plate space, the same space the packer
            places labels in).
        y: Bottom edge in inches.
        width: Box width in inches.
        height: Box height in inches.
        label: Legend text (the member's display name).
        color: Matplotlib colour token.
        style: Matplotlib dash pattern (``"--"``, ``"-.", ":"``, ...).
        line_width: Rectangle edge width in points.
    """

    x: float
    y: float
    width: float
    height: float
    label: str
    color: str
    style: str
    line_width: float = 1.2


# How far a label's overlay box is pulled inside its own slot, in inches, so
# the dashed key sits inboard of the label's border stroke instead of out in
# the scrap margin.
JOB_OVERLAY_INSET_IN = 0.05


def job_overlay_boxes(
    provenance: JobProvenance,
    packed_plates: Sequence[Any],
    plate_jobs: Mapping[int, Sequence[str]],
    *,
    inset: float = JOB_OVERLAY_INSET_IN,
) -> dict[int, list[JobOverlayBox]]:
    """Compute the per-label overlay boxes of every packed plate.

    Pure geometry (no matplotlib): the diagnostics layer draws what this
    returns, so the box maths is testable headless and on the Win7 target.

    One box per PLACEMENT, on EVERY plate: a jobset proof outlines each of its
    labels in that label's job colour, including on a sheet one job occupies
    alone. Boxes are NOT unioned per job - a job's labels sit in several places
    on a sheet, and the point of the overlay is to read a single label's
    ownership off the proof.

    Args:
        provenance: The jobset's composite-id side table (legend identity and
            colour index, both by jobset-file order).
        packed_plates: :class:`~plt_optimizer.generate.packing.layout.PackedPlate`
            objects in packing order (1-based plate numbers index them).
        plate_jobs: 1-based plate number -> the owning job keys of the labels
            on it, as recorded by the export in
            :attr:`~plt_optimizer.generate.vectorize.PerCutterExport.plate_jobs`.
        inset: How far each box is pulled inside its label's slot, in inches.
            Each axis clamps the margin to half the slot, so a label thinner
            than twice the margin degenerates to its centre line on that axis
            instead of inverting to a negative-size rectangle.

    Returns:
        1-based plate number -> its overlay boxes, grouped by job in
        jobset-file order and, within a job, in the packer's placement order.
        A plate carrying no placement whose owner is in ``plate_jobs`` maps to
        an empty list. A single-job run never calls this: ``provenance`` is
        ``None`` there, so its proofs stay byte-identical.
    """
    boxes: dict[int, list[JobOverlayBox]] = {}
    job_index = {key: index for index, key in enumerate(provenance.job_keys)}
    for plate_no, keys in plate_jobs.items():
        plate = packed_plates[plate_no - 1]
        known = set(keys)
        per_job: dict[str, list[tuple[float, float, float, float]]] = {}
        for placement in getattr(plate, "labels", ()) or ():
            source_id = str(getattr(getattr(placement, "source_label", None), "id", "") or "")
            owner = provenance.job_of_label(source_id)
            if owner is None or owner not in known:
                continue
            # The slot a rotated placement fills is the swapped pair, exactly
            # as plt_optimizer.generate.usage_stats._slot_rect computes it in
            # device units - the proof's box must cover the ink, not the
            # pre-rotation footprint.
            x_span, y_span = (
                (placement.height, placement.width)
                if placement.rotated
                else (placement.width, placement.height)
            )
            per_job.setdefault(owner, []).append(
                (placement.x, placement.y, placement.x + x_span, placement.y + y_span)
            )
        entries: list[JobOverlayBox] = []
        for key in sorted(per_job, key=lambda item: job_index.get(item, len(job_index))):
            record = provenance.record(key)
            color, style = legend_style_for(job_index.get(key, 0))
            name = record.display_name if record is not None else key
            for x_min, y_min, x_max, y_max in per_job[key]:
                span_x = x_max - x_min
                span_y = y_max - y_min
                # Inboard of the border stroke. Each axis clamps on its own, so
                # a label thinner than the margin collapses to a centre line
                # (a point on that axis) without distorting the other: the
                # inset can never invert to a negative-size rectangle.
                margin_x = min(inset, span_x / 2.0)
                margin_y = min(inset, span_y / 2.0)
                entries.append(
                    JobOverlayBox(
                        x=x_min + margin_x,
                        y=y_min + margin_y,
                        width=span_x - 2.0 * margin_x,
                        height=span_y - 2.0 * margin_y,
                        label=name,
                        color=color,
                        style=style,
                    )
                )
        boxes[plate_no] = entries
    return boxes


def parse_jobset_yaml(file_path: str | Path) -> JobSetSpec:
    """Parse and validate a jobset YAML file.

    Accepts both the compact list form::

        jobset:
          - job: path/to/job1.yaml

    and the extended mapping form (needed only to name the batch)::

        jobset:
          name: 2026-11-17 CES batch
          jobs:
            - job: path/to/job1.yaml

    Relative member paths resolve against the jobset file's directory, so a
    member job keeps working when the whole directory moves.

    Args:
        file_path: Path to the jobset YAML file.

    Returns:
        A validated :class:`JobSetSpec` with absolute member paths.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the root key is missing (a ``job:`` root reports that
            this is a single-job spec), the root shape is invalid, a jobset
            model field is unknown, or ``jobs`` is empty.
        yaml.YAMLError: For malformed YAML syntax.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Jobset file not found: {path}")

    try:
        with open(path, encoding="utf-8") as f:
            raw_data = yaml.safe_load(f)
    except OSError as exc:
        raise ValueError(f"Could not read jobset file {path}: {exc}") from exc

    if raw_data is None:
        raise ValueError("Empty YAML document")
    if not isinstance(raw_data, dict):
        raise ValueError("Jobset file must contain a mapping with a 'jobset' root element")
    if "job" in raw_data and "jobset" not in raw_data:
        raise ValueError(
            f"{path}: this is a single-job spec (root key 'job'); run "
            f"`plt-optimizer generate {path}`"
        )

    root = raw_data.get("jobset")
    if root is None:
        raise ValueError("Missing 'jobset' root element")

    payload: dict[str, Any]
    if isinstance(root, list):
        payload = {"jobs": root}
    elif isinstance(root, dict):
        payload = dict(root)
    else:
        raise ValueError("'jobset' must be a list of job entries or a mapping with a 'jobs' list")

    spec = JobSetSpec(**payload)
    base = path.parent
    jobs = [entry.model_copy(update={"job": _resolve(base, entry.job)}) for entry in spec.jobs]
    return spec.model_copy(update={"jobs": jobs})


def _resolve(base: Path, job_path: Path) -> Path:
    """Resolve a member job path against the jobset file's directory.

    Args:
        base: The jobset file's parent directory.
        job_path: The declared path (absolute paths pass through).

    Returns:
        The normalized absolute path.
    """
    if job_path.is_absolute():
        return job_path
    return (base / job_path).resolve()


def is_jobset_file(file_path: str | Path) -> bool:
    """Return ``True`` when a YAML file carries a ``jobset`` root element.

    Reads the raw mapping only (no validation), so it is safe to call on a
    file that may be a job spec, a jobset, or unparsable.

    Args:
        file_path: Path to the YAML file to probe.

    Returns:
        ``True`` when the document's root mapping has a ``jobset`` key.
    """
    path = Path(file_path)
    try:
        with open(path, encoding="utf-8") as f:
            raw_data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return False
    return isinstance(raw_data, dict) and "jobset" in raw_data


def evaluate_jobset_gate(
    contexts: Sequence[ResolvedJobContext],
) -> tuple[list[str], list[str]]:
    """Apply the jobset gate to prepared (un-namespaced) member specs.

    Checks the properties the merge cannot honour per member, in
    jobset-file order:

    * **layout** - the fill order of a shared sheet is one setting for the
      whole pool; members must agree (hard error).
    * **duplicate plate ids inside one member** - ambiguous once ids are
      namespaced per member (hard error).
    * **allow_rotation**, **job-level plate clearance** - pool-wide
      settings; the first member in file order wins (warning).
    * **pooled plate geometries** - members declaring the same
      ``(width, height, material)`` share sheets (informational warning).

    Args:
        contexts: The prepared member contexts, in jobset-file order.

    Returns:
        ``(errors, warnings)`` as human-readable lines.
    """
    if not contexts:
        return ["A jobset must contain at least one job"], []

    errors = _gate_hard_errors(contexts)
    warnings = _gate_pool_setting_warnings(contexts)
    warnings.extend(_gate_shared_sheet_warnings(contexts))
    return errors, warnings


def _gate_hard_errors(contexts: Sequence[ResolvedJobContext]) -> list[str]:
    """Return the gate's hard errors (layout agreement, plate-id shape).

    Args:
        contexts: The prepared member contexts, in jobset-file order.

    Returns:
        Human-readable error lines (empty when the batch may merge).
    """
    errors: list[str] = []
    first = contexts[0]

    # One fill order for one pool: a shared sheet is filled top-to-bottom
    # once, so a per-member layout cannot be honoured.
    for ctx in contexts[1:]:
        if ctx.layout is not first.layout:
            errors.append(
                f"layout conflict: '{ctx.record.display_name}' declares "
                f"'{getattr(ctx.layout, 'value', ctx.layout)}' but "
                f"'{first.record.display_name}' declares "
                f"'{getattr(first.layout, 'value', first.layout)}'; every job "
                "in a jobset must use the same layout"
            )

    for ctx in contexts:
        seen: set[str] = set()
        for plate in ctx.plates or ():
            if plate.id in seen:
                errors.append(
                    f"'{ctx.record.display_name}' declares plate id "
                    f"'{plate.id}' more than once; plate ids must be unique "
                    "within a job"
                )
            seen.add(plate.id)

    return errors


def _gate_pool_setting_warnings(contexts: Sequence[ResolvedJobContext]) -> list[str]:
    """Return warnings for pool-wide settings the members disagree on.

    The first member in jobset-file order wins for the whole pool (the
    merge has no per-member answer for these), and the run must say so.

    Args:
        contexts: The prepared member contexts, in jobset-file order.

    Returns:
        Human-readable warning lines.
    """
    first = contexts[0]
    warnings: list[str] = []

    if any(ctx.allow_rotation is not first.allow_rotation for ctx in contexts[1:]):
        values = ", ".join(f"{ctx.record.display_name}={ctx.allow_rotation}" for ctx in contexts)
        warnings.append(
            f"allow_rotation differs across members ({values}); "
            f"'{first.record.display_name}' wins for the whole pool"
        )
    if any(ctx.default_plate_clearance != first.default_plate_clearance for ctx in contexts[1:]):
        warnings.append(
            "job-level plate clearance differs across members; "
            f"'{first.record.display_name}' ({first.default_plate_clearance}) "
            "wins for auto-allocated sheets"
        )

    return warnings


def _gate_shared_sheet_warnings(contexts: Sequence[ResolvedJobContext]) -> list[str]:
    """Return the informational lines naming the sheet geometries that pool.

    Args:
        contexts: The prepared member contexts, in jobset-file order.

    Returns:
        One line per ``(width, height, material)`` geometry declared by more
        than one member, in stable geometry order.
    """
    # Owner labels are the display names, qualified with the composite key
    # when two members share a name (a job-key collision, which the caller
    # has already warned about) so the line never reads "by X, X".
    name_counts: dict[str, int] = {}
    for ctx in contexts:
        name_counts[ctx.record.display_name] = name_counts.get(ctx.record.display_name, 0) + 1

    def owner_label(ctx: ResolvedJobContext) -> str:
        name = ctx.record.display_name
        return f"{name}[{ctx.record.key}]" if name_counts[name] > 1 else name

    geometry_owners: dict[tuple[float, float, Optional[str]], list[str]] = {}
    for ctx in contexts:
        for plate in ctx.plates or ():
            geometry = (plate.width, plate.height, plate.material)
            geometry_owners.setdefault(geometry, []).append(owner_label(ctx))

    warnings: list[str] = []
    for (width, height, material), owners in sorted(
        geometry_owners.items(), key=lambda item: (item[0][0], item[0][1], item[0][2] or "")
    ):
        if len(owners) > 1:
            warnings.append(
                f"{width:g}x{height:g}in"
                + (f" {material}" if material else "")
                + " plates are declared by "
                + ", ".join(owners)
                + " - these sheets are packed as one pool"
            )
    return warnings


def namespace_job(
    job_key: str,
    prepared: PreparedJob,
) -> ResolvedJobContext:
    """Namespace one prepared member's label and plate ids.

    Args:
        job_key: The composite-id prefix for this member.
        prepared: The member's prepared job (ids as its own file declares
            them).

    Returns:
        A :class:`ResolvedJobContext` whose labels carry
        ``"{job_key}::{label_id}"`` ids (and whose ``plate_id`` pins point at
        namespaced plates), whose plates carry ``"{job_key}::{plate_id}"``
        ids, and whose :attr:`ResolvedJobContext.spec` is untouched (so the
        member's own report prints its own ids).
    """
    spec = prepared.job
    labels: list[ResolvedLabel] = []
    for label in prepared.resolved_labels:
        labels.append(
            replace(
                label,
                id=make_composite_id(job_key, label.id),
                plate_id=(
                    make_composite_id(job_key, label.plate_id)
                    if label.plate_id is not None
                    else None
                ),
            )
        )
    plates: Optional[tuple[PlateSpec, ...]] = None
    if spec.plates is not None:
        # PlateSpec is a Pydantic model (not a dataclass), so the composite
        # id is written with ``model_copy`` - a validated-cast-free update
        # that keeps every other plate field (and its nested models) intact.
        plates = tuple(
            plate.model_copy(update={"id": make_composite_id(job_key, plate.id)})
            for plate in spec.plates
        )
    return ResolvedJobContext(
        record=JobRecord(
            key=job_key,
            job_id=job_key,
            job_name=spec.job_name,
            spec_path=prepared.spec_path,
            label_ids=tuple(label.id for label in prepared.resolved_labels),
            plate_ids=tuple(plate.id for plate in (spec.plates or [])),
        ),
        spec=spec,
        prepared=prepared,
        resolved_labels=tuple(labels),
        plates=plates,
        plate_count=prepared.plate_count,
        layout=spec.layout,
        allow_rotation=spec.allow_rotation,
        default_plate_clearance=default_plate_clearance(spec),
    )


def _unique_job_key(job_name: str, taken: set[str]) -> tuple[str, Optional[str]]:
    """Return a unique composite-id key for a member job.

    Args:
        job_name: The member's declared ``job_name``.
        taken: Keys already used by earlier members (mutated by the caller).

    Returns:
        ``(key, collision_note)`` - ``collision_note`` is ``None`` when the
        sanitized name was already unique.
    """
    base = sanitize_job_id(job_name)
    if base not in taken:
        return base, None
    suffix = 2
    while f"{base}_{suffix}" in taken:
        suffix += 1
    key = f"{base}_{suffix}"
    return key, f"job key collision: '{base}' is taken; '{job_name}' uses '{key}'"


def build_jobset_plan(
    jobset_path: str | Path,
    *,
    job_config: Optional[JobConfig],
    job_config_path: Optional[Path] = None,
    inventory: Optional[List[float]] = None,
    boundary_hole_cutter: Optional[float] = None,
    stage_timings: Optional[StageTimings] = None,
) -> JobSetPlan:
    """Parse a jobset file, prepare every member and merge them into a plan.

    Members are prepared in jobset-file order (the determinism contract: the
    merged plate list, the packing pool and every shared file's segment order
    follow that order).

    Args:
        jobset_path: The jobset YAML file.
        job_config: Shop-level config shared by every member (the one
            ``job-config.json`` the run was given, or ``None``).
        job_config_path: Where that config was loaded from, forwarded to
            each member's parse step.
        inventory: Cutter sizes from ``tools.json`` (``None`` = ideal
            cutters).
        boundary_hole_cutter: Explicit boundary/hole cutter size in inches.
        stage_timings: Mutable ``{stage: seconds}`` collector; each member
            contributes its own ``parse`` / ``resolve`` entries under its
            stage tag.

    Returns:
        The merged :class:`JobSetPlan`.

    Raises:
        FileNotFoundError: If the jobset file is missing.
        ValueError: If the jobset file is unparsable.
        JobSetError: If the gate fails (the message lists every gate error).
        JobPreparationError: If a member fails to parse, expand, validate or
            resolve; its ``stage`` carries the member tag
            (``"resolve[seer_2963]"``).
    """
    path = Path(jobset_path)
    spec = parse_jobset_yaml(path)
    jobset_id = jobset_id_of(spec, path)

    contexts: list[ResolvedJobContext] = []
    taken_keys: set[str] = set()
    seen_paths: set[Path] = set()
    errors: list[str] = []
    warnings: list[str] = []

    for entry in spec.jobs:
        job_path = entry.job
        label = job_path.name
        if job_path in seen_paths:
            errors.append(f"duplicate job: {label} is listed more than once")
            continue
        seen_paths.add(job_path)
        if not job_path.exists():
            errors.append(f"job file not found: {job_path}")
            continue
        if is_jobset_file(job_path):
            errors.append(
                f"{label} is itself a jobset (root key 'jobset'); jobsets "
                "cannot be nested - list its jobs here"
            )
            continue
        try:
            prepared = prepare_job(
                job_path,
                job_config=job_config,
                job_config_path=job_config_path,
                inventory=inventory,
                boundary_hole_cutter=boundary_hole_cutter,
                stage_timings=stage_timings,
            )
        except JobPreparationError as exc:
            tag = sanitize_job_id(exc.job_name) if exc.job_name else job_path.stem
            raise JobPreparationError(
                f"{exc.stage}[{tag}]",
                exc.cause,
                console_message=exc.console_message,
                job_name=exc.job_name,
            ) from exc
        key, collision = _unique_job_key(prepared.job_name, taken_keys)
        taken_keys.add(key)
        if collision is not None:
            warnings.append(collision)
        contexts.append(namespace_job(key, prepared))
        LOG.debug("Jobset member prepared: %s (%s)", key, prepared.job_name)

    # The gate compares member contexts; with no context prepared there is
    # nothing to compare, and the per-file errors above already explain the
    # failure (appending "a jobset must contain at least one job" on top of
    # "job file not found" twice only buries the real message).
    gate_errors: list[str] = []
    gate_warnings: list[str] = []
    if contexts:
        gate_errors, gate_warnings = evaluate_jobset_gate(contexts)
    errors.extend(gate_errors)
    warnings.extend(gate_warnings)
    if errors:
        raise JobSetError(errors)

    first = contexts[0]
    provenance = JobProvenance(
        jobset_id=jobset_id,
        jobs=tuple(ctx.record for ctx in contexts),
        label_jobs={label.id: ctx.job_key for ctx in contexts for label in ctx.resolved_labels},
        plate_jobs={plate.id: ctx.job_key for ctx in contexts for plate in (ctx.plates or ())},
    )
    merged_labels = [label for ctx in contexts for label in ctx.resolved_labels]
    merged_plates: Optional[list[PlateSpec]] = [
        plate for ctx in contexts for plate in (ctx.plates or ())
    ]
    if not any(ctx.plates for ctx in contexts):
        merged_plates = None
    merged_plates = normalize_pinning(merged_plates, merged_labels)

    plan = JobSetPlan(
        jobset_id=jobset_id,
        jobset_name=spec.name or jobset_id,
        jobset_path=path,
        contexts=tuple(contexts),
        provenance=provenance,
        merged_labels=merged_labels,
        merged_plates=merged_plates,
        layout=first.layout,
        allow_rotation=first.allow_rotation,
        default_plate_clearance=first.default_plate_clearance,
        warnings=tuple(warnings),
    )
    LOG.info(
        "Jobset '%s': %d job(s), %d label(s), %s",
        jobset_id,
        plan.job_count,
        len(merged_labels),
        f"{len(merged_plates)} declared plate(s)" if merged_plates else "shared auto allocation",
    )
    return plan
