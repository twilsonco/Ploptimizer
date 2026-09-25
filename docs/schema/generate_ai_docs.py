#!/usr/bin/env python3
"""Generate AI-friendly reference artifacts for the YAML job specification.

The single source of truth is the Pydantic contract itself
(:class:`plt_optimizer.generate.schema.JobSpec` and
:class:`plt_optimizer.generate.job_config.JobDefaults`); this script renders
that contract into three artifacts written next to this file so the
documentation can never drift from the code:

- ``job_spec.schema.json``   JSON Schema for a job YAML's ``job:`` mapping
  (directly usable by ``jsonschema.validate`` or any JSON-Schema-aware AI).
- ``job_config.schema.json`` JSON Schema for ``job-config.json`` shop defaults.
- ``JOB_SPEC.md``            Dense markdown reference: per-model field
  tables, enum value tables, resolved fallback defaults, and the
  required-when-unconfigured set.

Hand-written semantics (job forms, cascade rules, expansion behaviour,
examples) live in ``README.md`` beside the generated artifacts.

Regenerate after any change to ``schema.py`` / ``job_config.py``::

    uv run python docs/schema/generate_ai_docs.py

The committed artifacts are pinned against drift by
``tests/test_job_spec_docs.py``. Output is deterministic (no timestamps).

Keep this script import-light (no matplotlib-transitive modules such as
``layout``/``label_renderer``); the plate-size constants are scraped from
``layout.py`` source text instead of imported.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # allow running as a plain script
    sys.path.insert(0, str(REPO_ROOT))

from plt_optimizer.generate import resolution  # noqa: E402
from plt_optimizer.generate.job_config import (  # noqa: E402
    REQUIRED_WHEN_UNCONFIGURED,
    JobDefaults,
)
from plt_optimizer.generate.schema import (  # noqa: E402
    DEFAULT_HOLE_DIAMETER,
    JobSpec,
)

OUTPUT_DIR = Path(__file__).resolve().parent
JOB_SPEC_JSON = OUTPUT_DIR / "job_spec.schema.json"
JOB_CONFIG_JSON = OUTPUT_DIR / "job_config.schema.json"
JOB_SPEC_MD = OUTPUT_DIR / "JOB_SPEC.md"

# Rendering order for the markdown model sections (root model first).
_MODEL_ORDER = ("JobSpec", "PlateSpec", "LabelSpec", "TextLine", "HoleSpec")
_ENUM_ORDER = ("HoleLocation", "TextHAlignment", "LayoutMode")

# JSON-Schema primitive -> Python-flavoured name (AI-consumer friendly).
_TYPE_NAMES = {"number": "float", "integer": "int", "string": "str", "boolean": "bool"}

# Curation notes for the resolved fallback constants scraped from
# ``resolution.py`` / ``schema.py`` / ``layout.py`` (the *values* always come
# from source; only the explanation is curated here).
_FALLBACK_NOTES: dict[str, str] = {
    "DEFAULT_TEXT_HEIGHT": "Font height when unset at line/label/job level.",
    "DEFAULT_MARGIN": "Label margin when unset at label/job level.",
    "DEFAULT_LINE_SPACING": "Extra line spacing when unset at line/label/job level.",
    "DEFAULT_HOLE_MARGIN": "Hole margin when unset everywhere. Required from config-or-spec when "
    "a job-config is in play.",
    "DEFAULT_MIN_HOLE_MARGIN": "None = collision avoidance may shrink hole_margin all the way "
    "to 0.0. Required from config-or-spec when a job-config is in play.",
    "DEFAULT_HOLE_TEXT_COLLISION_DISTANCE": "Engraved-stroke air gap on top of the stroke floor "
    "0.5 * (hole_cutter + text_cutter). Required from config-or-spec when a job-config is in "
    "play (the shop job-config.json currently sets 0.1).",
    "DEFAULT_BOUNDARY_HOLE_CUTTER": "Boundary/hole cutter diameter feeding the collision stroke "
    "floor; tools.json boundary_hole_cutter_size overrides it.",
    "DEFAULT_MAX_H_COMPRESS": "0.0 = horizontal compression disabled. Required from config-or-"
    "spec when a job-config is in play.",
    "DEFAULT_TEXT_H_ALIGNMENT": "Horizontal alignment fallback when unset everywhere.",
    "DEFAULT_HOLE_DIAMETER": "Drill-hole diameter when a hole entry omits 'diameter' (a "
    "job-config hole_diameter fills hole entries too).",
    "DEFAULT_PLATE_WIDTH": "Auto-allocated (unbounded mode) default plate width when job-config "
    "plate_width is unset. Required from config when a job-config is in play and plates are "
    "undeclared.",
    "DEFAULT_PLATE_HEIGHT": "Auto-allocated (unbounded mode) default plate height when job-config "
    "plate_height is unset. Required from config when a job-config is in play and plates are "
    "undeclared.",
}


# ---------------------------------------------------------------------------
# Schema builders (pure functions of the Pydantic models -> drift-testable)
# ---------------------------------------------------------------------------


def build_job_spec_schema() -> str:
    """Render the ``job:`` mapping JSON Schema as deterministic pretty JSON.

    Returns:
        Pretty-printed JSON Schema text for :class:`JobSpec` (trailing
        newline included).
    """
    return json.dumps(JobSpec.model_json_schema(), indent=2) + "\n"


def build_job_config_schema() -> str:
    """Render the ``job-config.json`` JSON Schema as deterministic pretty JSON.

    Returns:
        Pretty-printed JSON Schema text for :class:`JobDefaults` (trailing
        newline included).
    """
    return json.dumps(JobDefaults.model_json_schema(), indent=2) + "\n"


# ---------------------------------------------------------------------------
# Markdown rendering helpers
# ---------------------------------------------------------------------------


def _ref_name(ref: str) -> str:
    """Return the bare definition name of a ``$ref`` pointer.

    Args:
        ref: A JSON Schema ``$ref`` such as ``"#/$defs/HoleSpec"``.

    Returns:
        The trailing name (``"HoleSpec"``).
    """
    return ref.rsplit("/", 1)[-1]


def _type_label(node: dict[str, Any]) -> str:
    """Render a compact, AI-readable type label for a schema node.

    Args:
        node: A JSON Schema fragment (property or item position).

    Returns:
        A markdown-table-safe type string, e.g. ``float \\| null``,
        ``list[HoleSpec]`` or ``line \\| word`` (pipes escaped).
    """
    return _pipe_escape(_type_label_raw(node))


def _type_label_raw(node: dict[str, Any]) -> str:
    """Build the unescaped type label for a schema node (see `_type_label`).

    Args:
        node: A JSON Schema fragment (property or item position).

    Returns:
        The raw type string, possibly containing ``|`` separators.
    """
    if "$ref" in node:
        return _ref_name(node["$ref"])
    if "anyOf" in node:
        parts = [_type_label_raw(sub) for sub in node["anyOf"] if sub.get("type") != "null"]
        nullable = any(sub.get("type") == "null" for sub in node["anyOf"])
        label = " | ".join(parts)
        return f"{label} | null" if nullable else label
    if "enum" in node:
        return " | ".join(f"`{value}`" for value in node["enum"])
    if node.get("type") == "array":
        return f"list[{_type_label_raw(node.get('items', {}))}]"
    return _TYPE_NAMES.get(str(node.get("type")), "?")


def _pipe_escape(text: str) -> str:
    """Escape ``|`` characters so a string survives a markdown table cell.

    Args:
        text: Raw text possibly containing pipes.

    Returns:
        The text with every ``|`` replaced by ``\\|``.
    """
    return text.replace("|", "\\|")


def _constraints_label(node: dict[str, Any]) -> str:
    """Render numeric constraints (``ge``/``le``/``gt``/``lt``) as ``>=0`` etc.

    Args:
        node: A JSON Schema fragment (constraints may sit directly on it or
            inside its sole non-null ``anyOf`` branch).

    Returns:
        A space-joined constraint string, or an em dash when unconstrained.
    """
    if "anyOf" in node:
        branches = [sub for sub in node["anyOf"] if sub.get("type") != "null"]
        if len(branches) == 1:
            node = branches[0]
    parts: list[str] = []
    if "minimum" in node:
        parts.append(f">={node['minimum']:g}")
    if "maximum" in node:
        parts.append(f"<={node['maximum']:g}")
    if "exclusiveMinimum" in node:
        parts.append(f">{node['exclusiveMinimum']:g}")
    if "exclusiveMaximum" in node:
        parts.append(f"<{node['exclusiveMaximum']:g}")
    return " ".join(parts) if parts else "—"


def _default_label(node: dict[str, Any], required: bool) -> str:
    """Render a property's default value for the markdown table.

    Args:
        node: The property schema node.
        required: Whether the property is required (no default shown then).

    Returns:
        ``required`` for required fields, ``null (unset)`` for unsettable
        optional fields, otherwise the JSON literal.
    """
    if required:
        return "**required**"
    default = node.get("default", None)
    if "default" not in node or default is None:
        return "null (unset)"
    return f"`{json.dumps(default)}`"


def _md_cell(text: str) -> str:
    """Escape a string for use inside a markdown table cell.

    Args:
        text: Raw text (may contain newlines or pipes).

    Returns:
        Single-line, pipe-escaped text.
    """
    return text.replace("\n", " ").replace("|", "\\|").strip()


def _model_description(node: dict[str, Any]) -> str:
    """Return a model description with the docstring ``Attributes:`` block cut.

    The field tables already document every attribute, so the docstring
    attribute list is dropped; everything before ``Attributes:`` (and any
    ``Example:`` section) is kept.

    Args:
        node: A ``$defs`` model schema node.

    Returns:
        The trimmed single-paragraph-friendly description (may be empty).
    """
    description = node.get("description", "")
    for marker in ("\nAttributes:", "\nAttributes :"):
        if marker in description:
            description = description.split(marker, 1)[0]
    return description.strip()


def _enum_value_descriptions(node: dict[str, Any]) -> dict[str, str]:
    """Map enum values to their docstring descriptions.

    Pydantic puts the ``Attributes:`` docstring block of the Enum class into
    the schema ``description``; member names are normalised to values by
    lowercasing and swapping underscores for hyphens (``TOP_LEFT`` ->
    ``top-left``).

    Args:
        node: An enum schema node.

    Returns:
        Mapping of enum value -> description (empty when unparseable).
    """
    description = node.get("description", "")
    if "Attributes:" not in description:
        return {}
    block = description.split("Attributes:", 1)[1]
    result: dict[str, str] = {}
    current: Optional[str] = None
    for line in block.splitlines():
        match = re.match(r"^    (\w+): (.+)$", line)
        if match:
            current = match.group(1).lower().replace("_", "-")
            result[current] = match.group(2)
        elif current and re.match(r"^        \S", line):
            result[current] += " " + line.strip()
    return result


def _render_model(name: str, node: dict[str, Any], *, heading: bool = True) -> str:
    """Render one model section (description + field table).

    Args:
        name: The model name (``$defs`` key or root).
        node: The model schema node.
        heading: When True (default), prefix the section with an ``###``
            heading; the root model renders without one.

    Returns:
        A markdown section (heading optional).
    """
    required = set(node.get("required", []))
    lines = [f"### `{name}`", ""] if heading else []
    description = _model_description(node)
    if description:
        lines += [description, ""]
    lines += [
        "| Field | Type | Default | Constraints | Description |",
        "|---|---|---|---|---|",
    ]
    for field_name, field_node in node.get("properties", {}).items():
        lines.append(
            f"| `{field_name}` | {_type_label(field_node)} | "
            f"{_default_label(field_node, field_name in required)} | "
            f"{_constraints_label(field_node)} | {_md_cell(field_node.get('description', ''))} |"
        )
    return "\n".join(lines)


def _render_enum(name: str, node: dict[str, Any]) -> str:
    """Render one enum section (description + value table).

    Args:
        name: The enum name (``$defs`` key).
        node: The enum schema node.

    Returns:
        A markdown section starting with an ``###`` heading.
    """
    lines = [f"### `{name}`", ""]
    description = _model_description(node)
    if description:
        lines += [description, ""]
    value_docs = _enum_value_descriptions(node)
    lines += ["| Value | Description |", "|---|---|"]
    for value in node.get("enum", []):
        lines.append(f"| `{value}` | {_md_cell(value_docs.get(value, ''))} |")
    return "\n".join(lines)


def _scrape_constants() -> dict[str, str]:
    """Collect the resolved fallback constants from pipeline source files.

    Values are read from source (``resolution.py``, ``schema.py`` directly;
    ``layout.py`` by regex to avoid importing its matplotlib-transitive
    chain), so the rendered table can never show stale numbers.

    Returns:
        Mapping of constant name -> literal value as source text.
    """
    constants: dict[str, str] = {
        name: repr(getattr(resolution, name))
        for name in dir(resolution)
        if name.startswith("DEFAULT_")
    }
    constants["DEFAULT_HOLE_DIAMETER"] = repr(DEFAULT_HOLE_DIAMETER)
    layout_source = (REPO_ROOT / "plt_optimizer" / "generate" / "layout.py").read_text(
        encoding="utf-8"
    )
    for match in re.finditer(r"^(DEFAULT_PLATE_\w+): [^=]+= (.+)$", layout_source, re.MULTILINE):
        constants[match.group(1)] = match.group(2).strip()
    return constants


def build_job_spec_markdown() -> str:
    """Render the complete generated markdown reference.

    Returns:
        The full ``JOB_SPEC.md`` text (deterministic; trailing newline
        included).
    """
    schema = JobSpec.model_json_schema()
    defs: dict[str, Any] = schema.get("$defs", {})
    enum_names = [name for name in _ENUM_ORDER if name in defs] + sorted(
        name for name in defs if "enum" in defs[name] and name not in _ENUM_ORDER
    )
    model_names = [name for name in _MODEL_ORDER if name in defs] + sorted(
        name for name in defs if "enum" not in defs[name] and name not in _MODEL_ORDER
    )

    lines: list[str] = [
        "# YAML Job Specification — Generated Reference",
        "",
        "<!--",
        "AUTO-GENERATED by docs/schema/generate_ai_docs.py — DO NOT EDIT.",
        "Regenerate:  uv run python docs/schema/generate_ai_docs.py",
        "Pinned by:   tests/test_job_spec_docs.py",
        "Semantics that a schema cannot express (job forms, cascade rules,",
        "replacement-file expansion, examples) live in README.md beside this file.",
        "-->",
        "",
        "A job YAML file is a single top-level `job:` mapping; its value validates",
        "against the `JobSpec` model below. The machine-readable contract is",
        "[`job_spec.schema.json`](job_spec.schema.json) (this file's source of truth);",
        "`job-config.json` shop defaults validate against",
        "[`job_config.schema.json`](job_config.schema.json).",
        "",
        "Validation entry point: `plt_optimizer.generate.schema.parse_yaml(path)`",
        "(raises `ValueError`; `JobConfigError` for job-config violations). Every",
        "numeric field is in **inches**.",
        "",
        "## Root model: `job:` mapping",
        "",
        _render_model("JobSpec", schema, heading=False),
        "",
        "## Models",
        "",
    ]
    for name in model_names:
        lines += [_render_model(name, defs[name]), ""]

    lines += ["## Enums", ""]
    for name in enum_names:
        lines += [_render_enum(name, defs[name]), ""]

    lines += [
        "## Resolved fallback defaults",
        "",
        "When a cascading attribute is unset everywhere in its cascade (and no",
        "`job-config.json` supplies it), these hard-coded fallbacks apply",
        "(scraped from `resolution.py` / `schema.py` / `layout.py`). A value in",
        "`job-config.json` always takes precedence over a fallback; the YAML spec",
        "always beats the config.",
        "",
        "| Constant | Value | Note |",
        "|---|---|---|",
    ]
    for name, value in sorted(_scrape_constants().items()):
        lines.append(f"| `{name}` | `{value}` | {_md_cell(_FALLBACK_NOTES.get(name, '—'))} |")

    required_fields = sorted(REQUIRED_WHEN_UNCONFIGURED)
    lines += [
        "",
        "## Required-when-unconfigured (job-config gate)",
        "",
        "When a `--job-config` path is in play, these fields must be supplied by",
        "the config **or** the job spec (job level, or on every label); missing",
        "everywhere aborts parsing with `JobConfigError`:",
        "",
    ]
    lines += [f"- `{name}`" for name in required_fields]
    lines += [
        "- `plate_width` / `plate_height` (enforced separately: needed whenever",
        "  plates are undeclared, i.e. unbounded auto-allocation)",
        "",
        "Without a config path (direct API / test callers), every field keeps its",
        "fallback above and nothing is required.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Write all generated artifacts next to this script."""
    artifacts = {
        JOB_SPEC_JSON: build_job_spec_schema(),
        JOB_CONFIG_JSON: build_job_config_schema(),
        JOB_SPEC_MD: build_job_spec_markdown(),
    }
    for path, content in artifacts.items():
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)} ({len(content.splitlines())} lines)")


if __name__ == "__main__":
    main()
