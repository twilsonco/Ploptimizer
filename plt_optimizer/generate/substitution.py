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

Because items are produced by splitting on the delimiter, a replacement
item can never contain the delimiter itself: data that contains the
delimiter is *always* treated as separate items, so users must pick a
delimiter that does not occur inside single text lines (the EngraveLab
constraint, enforced by construction).

Example:
    >>> from pathlib import Path
    >>> from plt_optimizer.generate.schema import parse_yaml
    >>> from plt_optimizer.generate.substitution import expand_job_spec
    >>> job = parse_yaml("examples/replacement_job.yaml")
    >>> job = expand_job_spec(job, Path("examples/replacement_job.yaml"))
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from plt_optimizer.generate.schema import JobSpec, LabelSpec, TextLine

logger = logging.getLogger(__name__)

# Default item delimiter within a replacement file line (EngraveLab default).
DEFAULT_REPLACEMENT_DELIMITER: str = ";"

# Width of the zero-padded numeric suffix appended to expanded label ids.
_INSTANCE_SUFFIX_WIDTH: int = 4


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


def expand_job_spec(job: JobSpec, yaml_path: Path) -> JobSpec:
    """Expand all replacement-driven labels in a parsed job specification.

    Labels without ``replacement_text_file`` pass through untouched.
    Root-level single-label jobs (``content`` + ``count`` without a
    ``labels`` list) do not support replacement files and are returned
    unchanged.

    Args:
        job: The parsed :class:`~plt_optimizer.generate.schema.JobSpec`.
        yaml_path: Path to the job YAML file; its directory anchors
            relative ``replacement_text_file`` references.

    Returns:
        A new :class:`JobSpec` whose ``labels`` list contains one static
        label per replacement file line for every template label. When no
        label uses replacements, the original ``job`` is returned as-is.

    Raises:
        SubstitutionError: If any label's replacement file fails to load
            or apply (see :func:`expand_label_with_replacements`).
    """
    if not job.labels:
        return job

    if not any(label.replacement_text_file is not None for label in job.labels):
        return job

    base_dir = Path(yaml_path).resolve().parent
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
