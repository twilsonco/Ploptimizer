"""JSON-driven default values for YAML job specifications.

This module defines ``job-config.json`` — a shop-level companion to
``tools.json`` that supplies the *top-layer* default values the generate
pipeline would otherwise fall back to as hard-coded constants. A shop can
maintain one config per engraver / toolset and select it per run via the
``generate`` CLI's ``--job-config`` flag (default ``job-config.json``).

Semantics:

- Every configured default is applied at the **top-most layer** of the
  cascade: cascading attributes (margins, compression, collision
  distances, ...) are injected as *job-level* values, so the existing
  label -> job cascade works unchanged and labels still override them.
- ``plate_width`` / ``plate_height`` are *plate-layer* defaults: they fill
  plate entries that omit ``width`` / ``height`` and drive the unbounded
  (auto-allocated) bin size. They deliberately never fill the job-level
  label ``width`` / ``height`` (that would defeat label auto-sizing).
- ``left_clearance`` / ``top_clearance`` fill plate entries that omit the
  matching clearance, and shift placements on the unbounded auto-allocated
  bins. A job spec may omit ``plates:`` entirely: packing then runs in
  unbounded mode on config-sized default sheets, overflowing onto as many
  as needed.
- **Required-when-unconfigured:** the fields in
  :data:`REQUIRED_WHEN_UNCONFIGURED` must be provided either by this config
  or by the job spec (job level, or every label individually). When a
  config is in play and neither source supplies one of them, parsing fails
  with a clear error instead of silently applying a hard-coded default.
- A field that is missing from the JSON, explicitly ``null``, or whose
  config file is absent simply has no configured default (the required
  rule above then applies; fields outside the required set keep their
  in-code fallbacks).

Example:
    >>> from pathlib import Path
    >>> from plt_optimizer.generate.schema import parse_yaml
    >>> job = parse_yaml(
    ...     "tests_deps/test123_spec.yaml",
    ...     job_config_path=Path("job-config.json"),
    ... )
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from plt_optimizer.generate.schema import HoleSpec, LayoutMode, TextHAlignment

logger = logging.getLogger(__name__)

# Job-spec fields that lose their silent hard-coded fallback once a job
# config is in play: the value must come from the config or from the job
# spec itself (job level, or declared on every label). ``plate_width`` /
# ``plate_height`` are enforced separately (plate entries are validated
# structurally; unbounded auto-allocation needs the config values).
REQUIRED_WHEN_UNCONFIGURED: frozenset[str] = frozenset(
    {
        "max_h_compress",
        "hole_margin",
        "min_hole_margin",
        "hole_text_collision_distance",
    }
)


class JobConfigError(ValueError):
    """Raised when a job-config JSON file is invalid.

    Subclasses :class:`ValueError` so callers that already guard the
    pipeline with ``except ValueError`` keep working unchanged.
    """


class JobDefaults(BaseModel):
    """Configured default values from ``job-config.json``.

    Every field is optional; ``None`` (missing or explicit JSON ``null``)
    means "no configured default". Field names and validation mirror the
    corresponding :class:`~plt_optimizer.generate.schema.JobSpec` /
    :class:`~plt_optimizer.generate.schema.PlateSpec` fields. Unknown keys
    are rejected so typos in a shop config fail loudly.

    Attributes:
        text_height: Default font height in inches.
        character_spacing: Default extra spacing between characters (in
            inches). Unset falls back to the per-cutter derived default.
        line_spacing: Default extra spacing between text lines in inches.
        margin: Default label margin in inches.
        hole_margin: Default hole margin in inches.
        min_hole_margin: Default collision-avoidance floor for the hole
            margin in inches.
        hole_text_collision_distance: Default engraved-stroke air gap in
            inches between text and drill-hole strokes.
        max_h_compress: Default maximum horizontal compression fraction
            in ``[0.0, 1.0]``.
        text_h_alignment: Default horizontal text alignment.
        holes: Default drill-hole list (injected at the job layer; group
            locations expand exactly like spec-provided holes).
        hole_diameter: Default drill-hole diameter in inches, applied to
            hole entries (config- or spec-provided) that omit
            ``diameter``.
        allow_rotation: Default bin-packing rotation permission.
        text_chunk_mode: Default plate-space text optimization granularity.
        layout: Default plate fill order.
        plate_width: Default usable plate width in inches (plate layer).
        plate_height: Default usable plate height in inches (plate layer).
        left_clearance: Default plate left-edge clearance in inches.
        top_clearance: Default plate top-edge clearance in inches.
    """

    model_config = ConfigDict(extra="forbid")

    description: Optional[str] = Field(
        default=None,
        description="Free-form comment field (mirrors tools.json); not a default.",
    )
    text_height: Optional[float] = None
    character_spacing: Optional[float] = None
    line_spacing: Optional[float] = None
    margin: Optional[float] = Field(default=None, ge=0.0)
    hole_margin: Optional[float] = Field(default=None, ge=0.0)
    min_hole_margin: Optional[float] = Field(default=None, ge=0.0)
    hole_text_collision_distance: Optional[float] = Field(default=None, ge=0.0)
    max_h_compress: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    text_h_alignment: Optional[TextHAlignment] = None
    holes: Optional[list[HoleSpec]] = None
    hole_diameter: Optional[float] = Field(default=None, gt=0.0)
    allow_rotation: Optional[bool] = None
    text_chunk_mode: Optional[Literal["line", "word"]] = None
    layout: Optional[LayoutMode] = None
    plate_width: Optional[float] = Field(default=None, ge=0.0)
    plate_height: Optional[float] = Field(default=None, ge=0.0)
    left_clearance: Optional[float] = Field(default=None, ge=0.0)
    top_clearance: Optional[float] = Field(default=None, ge=0.0)


@dataclass(frozen=True)
class JobConfig:
    """A loaded job-config file with its provenance.

    Attributes:
        path: Path the config was loaded from.
        defaults: Validated default values.
    """

    path: Path
    defaults: JobDefaults


# Job-layer cascade fields mapped from config field -> job-spec key.
# ``plate_width``/``plate_height``/``left_clearance``/``top_clearance`` are
# plate-layer only (see module docstring) and ``hole_diameter`` is applied
# by hole-entry surgery, so none of them appear here.
_JOB_LAYER_FIELDS: tuple[str, ...] = (
    "text_height",
    "character_spacing",
    "line_spacing",
    "margin",
    "hole_margin",
    "min_hole_margin",
    "hole_text_collision_distance",
    "max_h_compress",
    "text_h_alignment",
    "allow_rotation",
    "text_chunk_mode",
    "layout",
)


def load_job_config(job_config_path: Optional[Path]) -> Optional[JobConfig]:
    """Load and validate a ``job-config.json`` file.

    Args:
        job_config_path: Path to the config file, or ``None`` to opt out of
            the config layer entirely (parsing then keeps the historical
            all-optional contract for direct API / test callers).

    Returns:
        The validated :class:`JobConfig`. When the path is given but the
        file does not exist, an :attr:`JobDefaults` with no configured
        values is returned — the config layer stays active, so the
        required-when-unconfigured contract still applies (mirroring the
        rule that a missing file means the spec must declare the fields).
        ``None`` is returned only when ``job_config_path`` is ``None``.

    Raises:
        JobConfigError: If the file exists but is not valid JSON, is not a
            JSON object, or fails :class:`JobDefaults` validation.
    """
    if job_config_path is None:
        return None

    if not job_config_path.is_file():
        logger.info(
            "Job config not found at %s; job spec must declare all required fields.",
            job_config_path,
        )
        return JobConfig(path=job_config_path, defaults=JobDefaults())

    try:
        raw_text = job_config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JobConfigError(f"Could not read job config {job_config_path}: {exc}") from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise JobConfigError(f"Invalid JSON in job config {job_config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise JobConfigError(
            f"Job config {job_config_path} must contain a JSON object, got {type(data).__name__}"
        )

    try:
        defaults = JobDefaults(**data)
    except ValidationError as exc:
        raise JobConfigError(f"Invalid job config {job_config_path}:\n{exc}") from exc

    logger.info("Loaded job defaults from %s", job_config_path)
    return JobConfig(path=job_config_path, defaults=defaults)


def _dump_hole_config(hole: HoleSpec) -> dict[str, Any]:
    """Serialize a config hole entry, preserving an omitted ``diameter``.

    :class:`HoleSpec` pre-fills ``diameter`` with its own default, which
    would shadow the configured ``hole_diameter``. Dropping the key when
    the user omitted it keeps the two defaults layered correctly
    (``hole_diameter`` wins over the schema default).

    Args:
        hole: The validated config hole entry.

    Returns:
        A plain mapping ready for injection into the raw job mapping.
    """
    payload = hole.model_dump(mode="json")
    if "diameter" not in hole.model_fields_set:
        payload.pop("diameter", None)
    return payload


def _hole_entries_with_diameters(holes: list[Any], default_diameter: Optional[float]) -> list[Any]:
    """Apply the configured default drill-hole diameter to hole entries.

    Args:
        holes: Raw ``holes`` list entries (dicts from YAML or config).
        default_diameter: Configured ``hole_diameter`` or ``None``.

    Returns:
        The entries with ``diameter`` filled in wherever it was omitted
        and a default is configured; unchanged otherwise.
    """
    if default_diameter is None:
        return holes
    result: list[Any] = []
    for entry in holes:
        if isinstance(entry, dict) and entry.get("diameter") is None:
            entry = {**entry, "diameter": default_diameter}
        result.append(entry)
    return result


def apply_job_config_defaults(
    job_data: dict[str, Any],
    config: Optional[JobConfig],
) -> dict[str, Any]:
    """Inject configured defaults into a raw job-spec mapping.

    Defaults are applied at the top-most layer only: cascading attributes
    fill missing *job-level* keys (the label -> job cascade then works
    unchanged), and plate defaults fill missing keys of each ``plates``
    entry. Values declared in the YAML are never overridden. The input
    mapping is not mutated.

    Args:
        job_data: The raw ``job`` mapping parsed from YAML.
        config: The loaded job config, or ``None`` (returned unchanged).

    Returns:
        A new mapping with the configured defaults applied.
    """
    if config is None:
        return job_data

    defaults = config.defaults
    filled: dict[str, Any] = dict(job_data)

    # An explicit YAML ``null`` counts as unset (the cascade treats None as
    # "inherit"), so config values fill absent keys and null-valued ones.
    for field_name in _JOB_LAYER_FIELDS:
        value = getattr(defaults, field_name)
        if value is not None and filled.get(field_name) is None:
            filled[field_name] = value

    if defaults.holes is not None and filled.get("holes") is None:
        filled["holes"] = [_dump_hole_config(hole) for hole in defaults.holes]

    if "holes" in filled and isinstance(filled["holes"], list):
        filled["holes"] = _hole_entries_with_diameters(filled["holes"], defaults.hole_diameter)

    plates = filled.get("plates")
    if isinstance(plates, list):
        plate_defaults = {
            "width": defaults.plate_width,
            "height": defaults.plate_height,
            "left_clearance": defaults.left_clearance,
            "top_clearance": defaults.top_clearance,
        }
        filled_plates: list[Any] = []
        for plate in plates:
            if isinstance(plate, dict):
                fill = {
                    key: value
                    for key, value in plate_defaults.items()
                    if value is not None and plate.get(key) is None
                }
                plate = {**fill, **plate}
            filled_plates.append(plate)
        filled["plates"] = filled_plates

    return filled


def assert_required_fields(
    job_data: Mapping[str, Any],
    config: Optional[JobConfig],
) -> None:
    """Enforce the required-when-unconfigured contract.

    Every field in :data:`REQUIRED_WHEN_UNCONFIGURED` must be present at
    the job layer of the (already default-filled) mapping, or declared on
    every label when a label list is used. ``plate_width`` /
    ``plate_height`` are checked separately: unbounded jobs (no ``plates``)
    need the configured plate size, since constrained plates already
    require their dimensions structurally.

    Args:
        job_data: The raw ``job`` mapping after default injection.
        config: The loaded job config, or ``None``. Enforcement is skipped
            entirely without a config (the historical all-optional
            contract for direct API / test callers).

    Raises:
        JobConfigError: If any required field is missing from both the
            config and the spec.
    """
    if config is None:
        return

    # An explicit YAML ``null`` counts as unset (the cascade treats None as
    # "inherit"), so only non-null values satisfy the contract.
    missing: list[str] = [
        name for name in sorted(REQUIRED_WHEN_UNCONFIGURED) if job_data.get(name) is None
    ]

    # A field declared on every label satisfies the contract too (the
    # cascade never needs the job-level value).
    labels = job_data.get("labels")
    if isinstance(labels, list) and labels:
        for name in list(missing):
            if all(isinstance(label, dict) and label.get(name) is not None for label in labels):
                missing.remove(name)

    # Plate dimensions: constrained jobs declare them per plate (already
    # structurally required); unbounded auto-allocation needs the config
    # values, which no job-spec field can supply.
    plate_size_missing: list[str] = []
    has_plates = isinstance(job_data.get("plates"), list) and bool(job_data.get("plates"))
    if not has_plates:
        if config.defaults.plate_width is None:
            plate_size_missing.append("plate_width")
        if config.defaults.plate_height is None:
            plate_size_missing.append("plate_height")

    if missing:
        raise JobConfigError(
            "Job spec is missing required field(s) "
            f"{', '.join(missing)}: declare them in the job spec or define "
            f"defaults in {config.path}"
        )
    if plate_size_missing:
        raise JobConfigError(
            "Job spec has no plates and the job config defines no default "
            f"plate size (missing {', '.join(plate_size_missing)} in "
            f"{config.path}): declare plates in the job spec or define "
            "plate_width / plate_height in the job config"
        )
