"""Replacement text file expansion for label templates.

This module implements EngraveLab / Vision Pro "badge" (a.k.a. "multiples")
style data-driven label generation. A ``LabelSpec`` may reference a
*replacement text file* whose lines each produce one label instance:

- Each line of the replacement file corresponds to one copy of the label
  template (the file's line count determines the label's effective count).
- Within a line, a configurable single-character delimiter (default ``;``)
  separates the replacement text items for the template's text lines.
- If a line contains *fewer* items than the template has lines, only that
  many lines are rendered (the rendered block is vertically centered by
  the existing render pipeline).
- If a line contains *more* items than the template has lines, the extra
  lines inherit the label-level text attributes.
- ``content`` is optional when a replacement file is used: without it,
  every rendered line inherits the label-level attributes; with it, each
  ``text`` value acts as a placeholder declaring that line's attributes
  (text height, alignment, spacing, ...).

Expansion happens as a preprocessing step between :func:`parse_yaml` and
:func:`~plt_optimizer.generate.resolution.resolve_job_spec`, so the
downstream resolution/packing/rendering pipeline is untouched: every
replacement-driven label is flattened into one fully-static
:class:`~plt_optimizer.generate.schema.LabelSpec` per file line with
``count=1`` and a unique ``id`` suffix (``base_0000``, ``base_0001``, ...).

Replacement files are also accepted at the **job** and **plate** levels:

- A job-level ``replacement_text_file`` replaces the ``labels`` section
  entirely: each file line synthesizes one label from the job-level label
  attributes (``width`` / ``height`` / ``text_height`` are required at the
  job level; job-level ``content`` acts as the per-line attribute
  template). Synthesized ids are ``label_0000``, ``label_0001``, ...
- A plate-level ``replacement_text_file`` synthesizes labels pinned to
  that plate (``LabelSpec.plate_id``): they pack exclusively onto the
  declaring plate, which accepts no other labels. Label attributes again
  come from the job-level cascade. Synthesized ids are
  ``<plate_id>_0000``, ...

Because items are produced by splitting on the delimiter, a replacement
item can never contain the delimiter itself: data that contains the
delimiter is *always* treated as separate items, so users must pick a
delimiter that does not occur inside single text lines (the EngraveLab
constraint, enforced by construction).

Example:
    >>> from pathlib import Path
    >>> from plt_optimizer.generate.schema import parse_yaml
    >>> from plt_optimizer.generate.substitution import expand_job_spec
    >>> job = parse_yaml("examples/job_specs/replacement_job.yaml")
    >>> job = expand_job_spec(job, Path("examples/job_specs/replacement_job.yaml"))
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from plt_optimizer.generate.schema import JobSpec, LabelSpec, PlateSpec, TextLine

logger = logging.getLogger(__name__)

# Default item delimiter within a replacement file line (EngraveLab default).
DEFAULT_REPLACEMENT_DELIMITER: str = ";"

# Width of the zero-padded numeric suffix appended to expanded label ids.
_INSTANCE_SUFFIX_WIDTH: int = 4

# Base id for labels synthesized by a job-level replacement text file
# (instances become ``label_0000``, ``label_0001``, ...).
JOB_LEVEL_LABEL_BASE_ID: str = "label"


class SubstitutionError(ValueError):
    """Raised when a replacement text file cannot be loaded or applied.

    Subclasses :class:`ValueError` so callers that already guard the
    pipeline with ``except ValueError`` keep working unchanged.
    """


def load_replacement_file(file_path: Path, delimiter: str) -> list[list[str]]:
    """Load a replacement text file and split it into per-instance item lists.

    Each non-trailing line of the file produces one list of text items,
    split on ``delimiter``. Items are used verbatim (no whitespace
    stripping), matching EngraveLab/Vision Pro semantics. Blank lines are
    preserved as single empty items (rendered as empty text) and logged at
    WARNING level. A trailing newline at end of file does *not* produce an
    extra instance.

    Args:
        file_path: Path to the replacement text file.
        delimiter: Single-character item delimiter.

    Returns:
        A list with one entry per file line; each entry is the list of
        delimited text items on that line.

    Raises:
        SubstitutionError: If the file does not exist, is not a regular
            file, cannot be read, or contains zero content lines.
    """
    if not file_path.is_file():
        raise SubstitutionError(f"Replacement text file not found: {file_path}")

    try:
        raw = file_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SubstitutionError(
            f"Replacement text file {file_path} is not valid UTF-8 text: {exc}"
        ) from exc
    except OSError as exc:
        raise SubstitutionError(f"Could not read replacement text file {file_path}: {exc}") from exc

    # Normalize Windows (\r\n) and classic Mac (\r) line endings, then drop
    # a single trailing newline so it does not create a phantom instance.
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    if normalized == "":
        raise SubstitutionError(f"Replacement text file is empty: {file_path}")

    lines = normalized.split("\n")
    instances: list[list[str]] = []
    for line_no, line in enumerate(lines, start=1):
        items = line.split(delimiter)
        if any(item == "" for item in items):
            logger.warning(
                "Replacement file %s line %d contains an empty text item; "
                "the corresponding rendered line will be blank.",
                file_path,
                line_no,
            )
        instances.append(items)

    logger.info(
        "Loaded replacement text file %s: %d instance line(s), delimiter %r.",
        file_path,
        len(instances),
        delimiter,
    )
    return instances


def _resolve_file_path(replacement_text_file: str, base_dir: Path) -> Path:
    """Resolve a replacement file reference against the job YAML directory.

    Args:
        replacement_text_file: The raw path string from the YAML spec.
        base_dir: Directory containing the job YAML file.

    Returns:
        The resolved path (absolute when the reference is absolute,
        otherwise relative to ``base_dir``).
    """
    candidate = Path(replacement_text_file)
    if candidate.is_absolute():
        return candidate
    return base_dir / candidate


def _synthesize_content(
    template_content: Optional[list[TextLine]],
    items: list[str],
) -> list[TextLine]:
    """Build the concrete text lines for one replacement instance.

    Args:
        template_content: The label's declared ``content`` lines (whose
            ``text`` values act as attribute placeholders), or ``None``
            when the label omitted ``content``.
        items: The delimited text items from one replacement file line.

    Returns:
        One :class:`TextLine` per item. Items up to the template length
        reuse the template line's attributes with the item as ``text``;
        extra items become new lines inheriting label-level attributes
        (all fields ``None``). Template lines beyond the item count are
        dropped (fewer items -> fewer rendered lines).
    """
    template = template_content or []
    content: list[TextLine] = []
    for index, item in enumerate(items):
        if index < len(template):
            content.append(template[index].model_copy(update={"text": item}))
        else:
            content.append(TextLine(text=item))
    return content


def expand_label_with_replacements(label: LabelSpec, base_dir: Path) -> list[LabelSpec]:
    """Expand one label specification into per-replacement-line labels.

    Args:
        label: The label specification to expand.
        base_dir: Directory used to resolve a relative
            ``replacement_text_file`` reference (the job YAML directory).

    Returns:
        A single-element list containing ``label`` unchanged when no
        ``replacement_text_file`` is configured; otherwise one new
        :class:`LabelSpec` per replacement file line, each with ``count=1``,
        a unique ``id`` suffix (``base_0000`` ...), synthesized ``content``,
        and the replacement fields cleared.

    Raises:
        SubstitutionError: If the replacement file is missing, unreadable,
            or empty (see :func:`load_replacement_file`).
    """
    if label.replacement_text_file is None:
        return [label]

    delimiter = label.replacement_text_delimiter or DEFAULT_REPLACEMENT_DELIMITER
    file_path = _resolve_file_path(label.replacement_text_file, base_dir)
    instances = load_replacement_file(file_path, delimiter)

    expanded: list[LabelSpec] = []
    for index, items in enumerate(instances):
        content = _synthesize_content(label.content, items)
        instance_id = f"{label.id}_{index:0{_INSTANCE_SUFFIX_WIDTH}d}"
        expanded.append(
            label.model_copy(
                update={
                    "id": instance_id,
                    "count": 1,
                    "content": content,
                    "replacement_text_file": None,
                    "replacement_text_delimiter": None,
                }
            )
        )

    logger.info(
        "Expanded label '%s' from replacement file %s: %d instance(s), %d template line(s).",
        label.id,
        file_path,
        len(expanded),
        len(label.content) if label.content is not None else 0,
    )
    return expanded


def _expand_job_level_replacements(job: JobSpec, base_dir: Path) -> JobSpec:
    """Expand a job-level replacement text file into the label list.

    The job-level file replaces the ``labels`` section: every file line
    synthesizes one label from the job-level attributes (the schema
    guarantees job-level ``width`` / ``height`` / ``text_height`` exist).
    The job-level ``content``, when present, acts as the per-line
    attribute template exactly like ``LabelSpec.content``.

    Args:
        job: The parsed job with ``replacement_text_file`` set.
        base_dir: Directory anchoring relative file references.

    Returns:
        A copy whose ``labels`` list holds one static label per file line
        and whose job-level replacement fields (and consumed ``content``)
        are cleared.

    Raises:
        SubstitutionError: If the replacement file fails to load (see
            :func:`expand_label_with_replacements`).
    """
    template = LabelSpec(
        id=JOB_LEVEL_LABEL_BASE_ID,
        content=job.content,
        replacement_text_file=job.replacement_text_file,
        replacement_text_delimiter=job.replacement_text_delimiter,
    )
    instances = expand_label_with_replacements(template, base_dir)
    logger.info(
        "Job '%s': job-level replacement file produced %d label(s).",
        job.job_name,
        len(instances),
    )
    return job.model_copy(
        update={
            "labels": instances,
            "content": None,
            "replacement_text_file": None,
            "replacement_text_delimiter": None,
        }
    )


def _expand_plate_level_replacements(job: JobSpec, base_dir: Path) -> JobSpec:
    """Expand plate-level replacement files into plate-pinned labels.

    Each plate declaring ``replacement_text_file`` produces one label per
    file line with ``plate_id`` set to the declaring plate (they pack
    exclusively onto it). Existing ``labels`` entries pass through
    label-level expansion unchanged and pack normally onto the unpinned
    plates. A root-level ``content`` acts as the per-line attribute
    template for the generated labels (mirroring the job-level
    replacement file), so it is consumed rather than materialized as a
    separate label. The plate replacement fields are cleared on the
    returned copy.

    Args:
        job: The parsed job with at least one plate-level file.
        base_dir: Directory anchoring relative file references.

    Returns:
        A copy whose ``labels`` list contains the base labels plus every
        plate-generated label, with replacement fields cleared.

    Raises:
        SubstitutionError: If any plate's replacement file fails to load
            (see :func:`expand_label_with_replacements`).
    """
    # With a labels list, content is absent (mutually exclusive); with the
    # root-level form, content is the shared attribute template.
    template_content = job.content if job.labels is None else None
    base_labels: list[LabelSpec] = list(job.labels) if job.labels else []

    # Label-level templates still expand (no-op for static labels).
    expanded_labels: list[LabelSpec] = []
    for label in base_labels:
        expanded_labels.extend(expand_label_with_replacements(label, base_dir))

    generated: list[LabelSpec] = []
    updated_plates: list[PlateSpec] = []
    for plate in job.plates or []:
        if plate.replacement_text_file is None:
            updated_plates.append(plate)
            continue
        # The job-level content acts as the attribute template only for a
        # job that has no explicit labels list (root-level form); with a
        # labels list, content is absent anyway.
        template = LabelSpec(
            id=plate.id,
            content=template_content,
            replacement_text_file=plate.replacement_text_file,
            replacement_text_delimiter=plate.replacement_text_delimiter,
        )
        instances = expand_label_with_replacements(template, base_dir)
        generated.extend(
            instance.model_copy(update={"plate_id": plate.id}) for instance in instances
        )
        logger.info(
            "Plate '%s': replacement file produced %d pinned label(s).",
            plate.id,
            len(instances),
        )
        updated_plates.append(
            plate.model_copy(
                update={
                    "replacement_text_file": None,
                    "replacement_text_delimiter": None,
                }
            )
        )

    return job.model_copy(
        update={
            "labels": expanded_labels + generated,
            "plates": updated_plates,
            "content": None,
            "count": None,
        }
    )


def expand_job_spec(job: JobSpec, yaml_path: Path) -> JobSpec:
    """Expand all replacement-driven labels in a parsed job specification.

    Handles replacement files at all three levels (job, plate, label).
    Labels without ``replacement_text_file`` pass through untouched.

    Args:
        job: The parsed :class:`~plt_optimizer.generate.schema.JobSpec`.
        yaml_path: Path to the job YAML file; its directory anchors
            relative ``replacement_text_file`` references.

    Returns:
        A new :class:`JobSpec` whose ``labels`` list contains one static
        label per replacement file line for every template (job-, plate-
        or label-level). When nothing uses replacements, the original
        ``job`` is returned as-is.

    Raises:
        SubstitutionError: If any replacement file fails to load or apply
            (see :func:`expand_label_with_replacements`).
    """
    base_dir = Path(yaml_path).resolve().parent

    if job.replacement_text_file is not None:
        return _expand_job_level_replacements(job, base_dir)

    has_plate_files = any(plate.replacement_text_file is not None for plate in job.plates or [])
    if has_plate_files:
        return _expand_plate_level_replacements(job, base_dir)

    if not job.labels:
        return job

    if not any(label.replacement_text_file is not None for label in job.labels):
        return job

    expanded_labels: list[LabelSpec] = []
    for label in job.labels:
        expanded_labels.extend(expand_label_with_replacements(label, base_dir))

    logger.info(
        "Job '%s': replacement expansion produced %d label(s) from %d specification(s).",
        job.job_name,
        len(expanded_labels),
        len(job.labels),
    )
    return job.model_copy(update={"labels": expanded_labels})
