"""Per-job preparation shared by ``generate`` and ``generate --jobset``.

The generate CLI's first phases — load ``job-config.json``, parse + expand
one job spec, resolve every label's geometry — live here so a jobset runs
each member job through *exactly* the code path a standalone run uses. The
CLI keeps the phases it owns (logging topology, the export/usage/report
tail, the console contract); this module keeps the per-job front half and
knows nothing about the console.

Failure handling is data, not printing: :func:`prepare_job` raises
:class:`JobPreparationError` carrying the pipeline stage that failed and the
single-job CLI's stderr line for that shape of failure, so the CLI can
reproduce its historical console contract verbatim while a jobset run
aggregates every member's failure into one root report.

Import weight follows the CLI's rule: this module is imported at
``plt-optimizer`` startup (the router builds every subparser), so it pulls
only the light generation modules. The heavy runtime (numpy/matplotlib/
vpype through ``label_renderer``) is imported *inside* :func:`prepare_job`,
between the ``parse`` and ``resolve`` stage timers — the same position the
CLI used before this extraction, so the per-stage timings in
``generation_report/report.json`` are unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple

from plt_optimizer.core.filenames import sanitize_filename_component
from plt_optimizer.generate.resolution import CutterSizeError, resolve_job_spec
from plt_optimizer.generate.schema import JobSpec, parse_yaml
from plt_optimizer.generate.substitution import SubstitutionError, expand_job_spec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from plt_optimizer.generate.job_config import JobConfig
    from plt_optimizer.generate.resolution import ResolvedLabel
    from plt_optimizer.utils.stage_timing import StageTimings

__all__ = [
    "JobPreparationError",
    "PreparedJob",
    "prepare_job",
    "sanitize_job_id",
]

_LOG = logging.getLogger(__name__)


def sanitize_job_id(job_name: str) -> str:
    """Build the filename-safe id used in per-cutter PLT/PDF names.

    Delegates to :func:`plt_optimizer.core.filenames.sanitize_filename_component`:
    CJK (and any other non-ASCII) job names are replaced by ``job`` so a
    Chinese spec never produces an untranscodable filename. The full
    ``job_name`` stays in the headers and plot titles.

    Args:
        job_name: The job's display name.

    Returns:
        The ASCII filename component.
    """
    return sanitize_filename_component(job_name, fallback="job")


class JobPreparationError(Exception):
    """A job failed before a single plate was packed.

    Attributes:
        stage: The pipeline stage the failure happened in (``parse``,
            ``expand`` or ``resolve``).
        cause: The underlying exception, kept so the failure report can
            record its real type and message.
        console_message: The stderr line the standalone CLI prints for
            this shape of failure. Empty for failures that route through
            the generic "Generation FAILED" report path (``resolve``).
        job_name: The job's display name when the failure happened after
            its spec was parsed (``""`` when the file itself could not be
            read), so a jobset run can tag the failure with its member.
    """

    def __init__(
        self,
        stage: str,
        cause: BaseException,
        console_message: str = "",
        job_name: str = "",
    ) -> None:
        """Record the stage, the underlying error and its console phrasing.

        Args:
            stage: The pipeline stage the failure happened in.
            cause: The underlying exception.
            console_message: The standalone CLI's stderr line (empty when
                the generic failure line applies).
            job_name: The job's display name when it is already known.
        """
        super().__init__(str(cause))
        self.stage = stage
        self.cause = cause
        self.console_message = console_message
        self.job_name = job_name


@dataclass
class PreparedJob:
    """One job resolved to geometry, ready to be packed (alone or pooled).

    Attributes:
        spec_path: The job spec file the data came from.
        job: The parsed, expanded (placement-flattened) job spec.
        job_name: The job's display name.
        job_id: The filename-safe job id (``sanitize_job_id``).
        plate_count: Plates the job declared (0 when unbounded).
        unique_labels: Labels the job declared, post-expansion.
        resolved_labels: Every label resolved to real geometry.
        tool_option_overrides: The job's ``tool_options`` block, kept so a
            jobset export can emit each job's own tool-option headers on
            shared layers.
    """

    spec_path: Path
    job: JobSpec
    job_name: str
    job_id: str
    plate_count: int
    unique_labels: int
    resolved_labels: List[ResolvedLabel] = field(default_factory=list)

    @property
    def tool_option_overrides(self) -> Optional[object]:
        """The job-level ``tool_options`` block (``None`` when unset)."""
        return self.job.tool_options


def prepare_job(
    spec_path: Path,
    *,
    job_config: Optional[JobConfig],
    job_config_path: Optional[Path] = None,
    inventory: Optional[List[float]] = None,
    boundary_hole_cutter: Optional[float] = None,
    stage_timings: Optional[StageTimings] = None,
) -> PreparedJob:
    """Parse, expand and resolve one job spec.

    Runs the pipeline phases that are per-job and produce no files:
    ``parse`` (``job-config.json`` defaults + YAML + placement expansion),
    then ``resolve`` (cutter sizing, typography, per-line geometry).

    Args:
        spec_path: The job specification YAML to load.
        job_config: The loaded shop defaults (``job-config.json``), or
            ``None`` when no config is in play.
        job_config_path: Where ``job-config.json`` was loaded from, passed
            through to :func:`~plt_optimizer.generate.schema.parse_yaml`.
        inventory: Real cutter diameters from ``tools.json`` (``None``
            selects ideal cutters).
        boundary_hole_cutter: Requested boundary/hole cutter size in
            inches, or ``None`` to pick the finest available.
        stage_timings: The per-stage timing collector (``None`` skips
            collection).

    Returns:
        The :class:`PreparedJob` for ``spec_path``.

    Raises:
        JobPreparationError: The spec failed to parse, expand, validate or
            resolve; the error carries the stage name.
    """
    parsed_job_name = ""
    try:
        from plt_optimizer.utils.stage_timing import stage_timer

        with stage_timer("parse", _LOG, timings=stage_timings):
            job = parse_yaml(spec_path, job_config_path=job_config_path)
            parsed_job_name = job.job_name
            # Flatten replacement-driven labels (EngraveLab "badge"/multiples)
            # into static LabelSpecs before resolution, per the schema contract.
            job = expand_job_spec(job, spec_path)
    except JobPreparationError:
        raise
    except SubstitutionError as exc:
        raise JobPreparationError(
            "expand",
            exc,
            console_message=f"Error expanding replacement text file: {exc}",
            job_name=parsed_job_name,
        ) from exc
    except Exception as exc:  # noqa: BLE001 - every parse failure is one report shape
        raise JobPreparationError(
            "parse",
            exc,
            console_message=f"Error parsing specification: {exc}",
            job_name=parsed_job_name,
        ) from exc

    plate_count = len(job.plates) if job.plates is not None else 0
    unique_labels = len(job.labels) if job.labels is not None else 0
    job_name = job.job_name
    job_id = sanitize_job_id(job.job_name)
    _LOG.info(
        "Loaded %s: %d plates, %d unique labels. Output will be written to: <caller>",
        job_name,
        plate_count,
        unique_labels,
    )

    # Lazy heavy import (see the module docstring). The root logger's
    # handlers are snapshotted first: some third-party libraries (vpype)
    # install a root handler of their own on import, and the pipeline
    # logger propagates to the root -- so the extra handler would print
    # every record a second time, in a foreign format. The handlers that
    # exist before the import (the test suite's capture handler, a
    # caller's own) are kept.
    root_handlers = list(logging.getLogger().handlers)
    from plt_optimizer.generate.label_renderer import measure_line_vertical_extents
    from plt_optimizer.generate.resolution import memoize_extents_probe

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if handler not in root_handlers:
            root_logger.removeHandler(handler)

    try:
        from plt_optimizer.utils.stage_timing import stage_timer

        with stage_timer("resolve", _LOG, timings=stage_timings):
            resolved_labels: List[ResolvedLabel] = resolve_job_spec(
                job,
                available_cutters=inventory,
                boundary_hole_cutter_size=boundary_hole_cutter,
                # Baseline spacing measures each line's real descender extent so
                # auto spacing and margin fitting account for it (the probe is
                # memoized and is never called for single-line labels, labels
                # stacking by ink box, or jobs without a multi-line label).
                extents_probe=memoize_extents_probe(measure_line_vertical_extents),
            )
    except CutterSizeError as exc:
        raise JobPreparationError("resolve", exc, job_name=job_name) from exc

    return PreparedJob(
        spec_path=spec_path,
        job=job,
        job_name=job_name,
        job_id=job_id,
        plate_count=plate_count,
        unique_labels=unique_labels,
        resolved_labels=resolved_labels,
    )


def default_plate_size(job_config: Optional[JobConfig]) -> Optional[Tuple[float, float]]:
    """Extract the ``(width, height)`` default plate size from a job config.

    Args:
        job_config: The loaded job config, or ``None``.

    Returns:
        The configured ``(plate_width, plate_height)`` when both are set,
        otherwise ``None`` (callers keep their historical defaults).
    """
    if job_config is None:
        return None
    defaults = job_config.defaults
    if defaults.plate_width is None or defaults.plate_height is None:
        return None
    return (defaults.plate_width, defaults.plate_height)


def default_plate_clearance(job: JobSpec) -> Optional[Tuple[float, float]]:
    """Extract the ``(left, top)`` default plate clearance from a job spec.

    Reads the job-level ``left_clearance`` / ``top_clearance`` (which
    already include ``job-config.json`` injection via ``parse_yaml``).

    Args:
        job: The parsed job specification.

    Returns:
        The job-level ``(left_clearance, top_clearance)`` when at least
        one is non-zero, otherwise ``None`` (no shift on auto-allocated
        plates; the fields default to ``0.0``).
    """
    left = job.left_clearance or 0.0
    top = job.top_clearance or 0.0
    if not left and not top:
        return None
    return (left, top)
