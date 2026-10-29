#!/usr/bin/env python3
"""Validate a job spec YAML (pre-flight for ``plt-optimizer generate``).

The authoritative gate is
:func:`plt_optimizer.generate.schema.parse_yaml`, which applies the full
contract: YAML syntax, the ``job:`` root, ``job-config.json`` default
injection, the required-when-unconfigured rule, and every Pydantic model
validator (unknown-key paths, enums, ranges, cross-field rules). The generate
CLI runs it too, so this script adds no new spec rules — it is a fast pre-flight
that checks a spec without rendering typography, writing PLT/PDF output, or
needing the plotter, and it adds one check the parser cannot do:

- ``replacement_text_file`` existence at every tier (job, plate, label),
  resolved relative to the spec directory exactly as the pipeline resolves it.

Run with::

    uv run python scripts/validate_job_spec.py examples/job_specs/my_job.yaml
    uv run python scripts/validate_job_spec.py my_job.yaml --job-config cfg.json
    uv run python scripts/validate_job_spec.py my_job.yaml --json

This file lives in the repo ``scripts/`` directory and is mirrored byte-for-byte
into the ``validating-jobs`` Hermes skill (``validating-jobs/scripts/``), so it
locates the pipeline itself instead of assuming a fixed depth below a repo root:
an importable ``plt_optimizer`` (installed package, ``uv run`` in a checkout,
``PYTHONPATH``) is used as-is, otherwise the nearest checkout root above this
file is added to ``sys.path``. The ``--job-config`` / ``--tools`` defaults are
cwd-relative, exactly like ``plt-optimizer generate``, so run it beside the job
files (or pass the paths).

Exit codes: ``0`` valid · ``1`` validation errors · ``2`` a file could not be
read (spec absent, unreadable, or malformed YAML), or the pipeline could not be
located.

Stdlib + ``plt_optimizer`` only: ``jsonschema`` is a dev extra and is not
installed on the Windows 7 plotter PC this pipeline deploys to.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

EXIT_OK = 0
EXIT_ERRORS = 1
EXIT_FILE_ERROR = 2

#: Files that together identify a PLT-Optimizer checkout root.
_REPO_ROOT_MARKERS: tuple[str, ...] = ("pyproject.toml", "plt_optimizer/__init__.py")

#: Actionable guidance printed (exit ``2``) when the pipeline cannot be located.
_PIPELINE_NOT_FOUND = (
    "cannot import plt_optimizer - this helper is a thin CLI over the pipeline's\n"
    "own validation gates, so it needs the package. Fix it one of two ways:\n"
    "  1. inside a checkout: run it with uv from the repo root\n"
    "       uv run python scripts/validate_job_spec.py ...\n"
    "  2. anywhere else (e.g. an installed skill): install the package\n"
    "       uv tool install plt-optimizer   (or: pip install plt-optimizer)\n"
    "     then run it beside the spec/job-config.json/tools.json it validates,\n"
    "     or pass --job-config / --tools."
)


def find_repo_root(start: Path) -> Optional[Path]:
    """Find the nearest PLT-Optimizer checkout root at or above ``start``.

    Args:
        start: Directory (or file inside one) to start the upward walk from.

    Returns:
        The directory containing every marker in :data:`_REPO_ROOT_MARKERS`, or
        ``None`` when no ancestor qualifies.
    """
    directory = start if start.is_dir() else start.parent
    for candidate in (directory, *directory.parents):
        if all((candidate / marker).exists() for marker in _REPO_ROOT_MARKERS):
            return candidate
    return None


def bootstrap_sys_path(start: Optional[Path] = None) -> None:
    """Make ``plt_optimizer`` importable from wherever this script lives.

    Args:
        start: Path to treat as this script's location (defaults to
            ``__file__``); a parameter so the walk is unit-testable.

    Raises:
        SystemExit: Exit code ``2`` with :data:`_PIPELINE_NOT_FOUND` when the
            package is neither importable nor reachable from a checkout root.
    """
    if importlib.util.find_spec("plt_optimizer") is not None:
        return
    origin = Path(__file__).resolve() if start is None else Path(start).resolve()
    repo_root = find_repo_root(origin)
    if repo_root is None:
        print(
            f"{_PIPELINE_NOT_FOUND}\n(looked for a PLT-Optimizer checkout above {origin})",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_FILE_ERROR)
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


bootstrap_sys_path()

import yaml  # noqa: E402

from plt_optimizer.generate.job_config import JobConfigError  # noqa: E402
from plt_optimizer.generate.schema import JobSpec, parse_yaml  # noqa: E402
from plt_optimizer.generate.substitution import _resolve_file_path  # noqa: E402

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    """One validation result.

    Attributes:
        severity: ``"error"`` or ``"warning"``.
        message: Human-readable description of the problem.
    """

    severity: str
    message: str

    def as_dict(self) -> dict[str, str]:
        """Serialize for ``--json`` output.

        Returns:
            A mapping of severity -> message.
        """
        return {"severity": self.severity, "message": self.message}


def _error(message: str) -> Finding:
    """Build an error finding.

    Args:
        message: The problem description.

    Returns:
        The finding.
    """
    return Finding(SEVERITY_ERROR, message)


def _warning(message: str) -> Finding:
    """Build a warning finding.

    Args:
        message: The problem description.

    Returns:
        The finding.
    """
    return Finding(SEVERITY_WARNING, message)


def collect_replacement_files(job: JobSpec) -> list[tuple[str, str]]:
    """Gather every ``replacement_text_file`` reference in a parsed job.

    Args:
        job: The validated job spec.

    Returns:
        Pairs of (tier label, raw path string) in declaration order: job,
        then each plate, then each label (and each label nested in a plate).
    """
    refs: list[tuple[str, str]] = []
    if job.replacement_text_file:
        refs.append(("job", job.replacement_text_file))
    for plate_index, plate in enumerate(job.plates or []):
        if plate.replacement_text_file:
            refs.append((f"plates[{plate_index}]", plate.replacement_text_file))
    for label_index, label in enumerate(job.labels or []):
        if label.replacement_text_file:
            refs.append((f"labels[{label_index}]", label.replacement_text_file))
    return refs


def check_replacement_files(job: JobSpec, spec_path: Path) -> list[Finding]:
    """Report every ``replacement_text_file`` that does not exist on disk.

    Paths resolve relative to the spec file's directory (the pipeline's own
    rule), so a batch job's data file is checked exactly as it will be read.

    Args:
        job: The validated job spec.
        spec_path: Path to the spec file (its parent is the base directory).

    Returns:
        One error per missing data file, naming the resolved path.
    """
    base_dir = spec_path.resolve().parent
    findings: list[Finding] = []
    for tier, raw in collect_replacement_files(job):
        resolved = _resolve_file_path(raw, base_dir)
        if not resolved.is_file():
            findings.append(
                _error(f"{tier}.replacement_text_file: not found: {resolved} (declared as {raw!r})")
            )
    return findings


def check_tools(tools_path: Optional[Path]) -> list[Finding]:
    """Validate the cutter inventory JSON the generate CLI loads.

    ``tools.json`` has no JSON Schema; the pipeline reads exactly two keys
    (``available_cutters``, ``boundary_hole_cutter_size``), so this checks
    presence, JSON syntax, and the types of those keys.

    Args:
        tools_path: Path to the inventory, or ``None`` to skip the check.

    Returns:
        Errors for an unreadable/invalid inventory; a warning when the file
        is absent (the pipeline then runs without a cutter ladder).
    """
    if tools_path is None:
        return []
    if not tools_path.is_file():
        return [
            _warning(
                f"{tools_path}: cutter inventory not found; the run uses no "
                f"available_cutters ladder"
            )
        ]
    try:
        data = json.loads(tools_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [_error(f"{tools_path}: unreadable cutter inventory: {exc}")]
    if not isinstance(data, dict):
        return [_error(f"{tools_path}: cutter inventory must be a JSON object")]

    findings: list[Finding] = []
    inventory = data.get("available_cutters")
    if inventory is not None:
        if not isinstance(inventory, list) or not all(
            isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0
            for size in inventory
        ):
            findings.append(
                _error(f"{tools_path}: available_cutters must be a list of positive numbers")
            )
    boundary = data.get("boundary_hole_cutter_size")
    if boundary is not None and (
        not isinstance(boundary, (int, float)) or isinstance(boundary, bool) or boundary <= 0
    ):
        findings.append(
            _error(f"{tools_path}: boundary_hole_cutter_size must be a positive number")
        )
    return findings


@dataclass(frozen=True)
class SpecReport:
    """Result of validating one job spec.

    Attributes:
        findings: Every error/warning discovered.
        job: The parsed spec, or ``None`` when parsing failed.
        file_error: True when the spec/config file could not be read at all
            (missing spec, unreadable, malformed YAML), which maps to exit 2.
    """

    findings: list[Finding]
    job: Optional[JobSpec]
    file_error: bool = False


def validate_spec(spec_path: Path, job_config_path: Optional[Path]) -> SpecReport:
    """Parse and validate one job spec, then run the pre-flight checks.

    Args:
        spec_path: Path to the job spec YAML.
        job_config_path: Shop defaults JSON, or ``None`` to opt out of the
            config layer (the historical all-optional contract).

    Returns:
        The report.
    """
    if not spec_path.is_file():
        return SpecReport([_error(f"specification file not found: {spec_path}")], None, True)

    try:
        job = parse_yaml(spec_path, job_config_path=job_config_path)
    except FileNotFoundError as exc:
        return SpecReport([_error(str(exc))], None, True)
    except yaml.YAMLError as exc:
        return SpecReport([_error(f"malformed YAML in {spec_path}: {exc}")], None, True)
    except JobConfigError as exc:
        # The config layer is the upstream gate: report it and stop, since the
        # spec cannot be meaningfully checked against defaults that do not load.
        return SpecReport([_error(str(exc))], None)
    except (ValueError, TypeError) as exc:
        # pydantic ValidationError subclasses ValueError; parse_yaml raises
        # ValueError for the structural rules (missing `job:` root, empty doc).
        return SpecReport([_error(str(exc))], None)

    return SpecReport(check_replacement_files(job, spec_path), job)


def count_errors(findings: Sequence[Finding]) -> int:
    """Count findings that must fail the run.

    Args:
        findings: The findings to tally.

    Returns:
        The number of error-severity findings.
    """
    return sum(1 for finding in findings if finding.severity == SEVERITY_ERROR)


def format_text(path: Path, findings: Sequence[Finding], job: Optional[JobSpec]) -> str:
    """Render findings as human-readable lines.

    Args:
        path: The spec path being reported on.
        findings: The findings to render.
        job: The parsed spec when parsing succeeded.

    Returns:
        The report text (no trailing newline).
    """
    lines: list[str] = []
    for finding in findings:
        lines.append(f"{finding.severity.upper():7s} {path}: {finding.message}")
    if not lines:
        summary = ""
        if job is not None:
            summary = f" ({len(job.labels or [])} label(s), {len(job.plates or [])} plate(s))"
        lines.append(f"OK {path}: job spec is valid{summary}")
    return "\n".join(lines)


def format_json(path: Path, findings: Sequence[Finding], job: Optional[JobSpec]) -> str:
    """Render findings as a JSON document.

    Args:
        path: The spec path being reported on.
        findings: The findings to render.
        job: The parsed spec when parsing succeeded.

    Returns:
        A JSON string.
    """
    payload: dict[str, Any] = {
        "path": str(path),
        "ok": count_errors(findings) == 0,
        "errors": sum(1 for f in findings if f.severity == SEVERITY_ERROR),
        "warnings": sum(1 for f in findings if f.severity == SEVERITY_WARNING),
        "findings": [finding.as_dict() for finding in findings],
    }
    if job is not None:
        payload["job_name"] = job.job_name
        payload["label_count"] = len(job.labels or [])
        payload["plate_count"] = len(job.plates or [])
    return json.dumps(payload, indent=2)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="validate_job_spec.py",
        description="Validate a job spec YAML without running generation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes: 0 valid | 1 validation errors | "
            "2 file unreadable, or the pipeline could not be located"
        ),
    )
    parser.add_argument("spec", type=Path, help="Path to the job spec YAML.")
    parser.add_argument(
        "--job-config",
        type=str,
        # Cwd-relative, exactly like `generate --job-config`: run the helper beside
        # the job files, or pass an explicit path.
        default="job-config.json",
        help=(
            "Shop defaults JSON, exactly as `generate --job-config` uses it "
            "(default: job-config.json in the CWD). Pass an empty value to opt "
            "out of the config layer entirely."
        ),
    )
    parser.add_argument(
        "--tools",
        type=str,
        default="tools.json",
        help=(
            "Cutter inventory JSON (default: tools.json in the CWD). Pass an "
            "empty value to skip the inventory check."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON report instead of text.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser


def _opt_path(raw: Optional[str]) -> Optional[Path]:
    """Normalize a string CLI option that may be an explicit empty value.

    ``Path("")`` compares equal to ``Path(".")``, which is truthy, so the
    usual `value or None` idiom cannot express "opt out". An empty string is
    the documented opt-out sentinel, which is why these options are parsed as
    strings and converted here.

    Args:
        raw: The parsed value, if any.

    Returns:
        ``None`` when the user passed an empty value, else the path.
    """
    if raw is None or raw == "":
        return None
    return Path(raw)


def run(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        The process exit code.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    spec_path: Path = args.spec
    job_config_path = _opt_path(args.job_config)
    tools_path = _opt_path(args.tools)

    report = validate_spec(spec_path, job_config_path)
    findings = list(report.findings)
    if report.job is not None:
        findings.extend(check_tools(tools_path))
    # Stable sort on severity only: errors first, and within a severity the
    # spec's own problems stay ahead of the companion-file checks.
    findings.sort(key=lambda f: f.severity != SEVERITY_ERROR)

    if args.json:
        print(format_json(spec_path, findings, report.job))
    else:
        print(format_text(spec_path, findings, report.job))
        n_errors = count_errors(findings)
        n_warnings = sum(1 for f in findings if f.severity == SEVERITY_WARNING)
        if n_errors or n_warnings:
            print(f"{n_errors} error(s), {n_warnings} warning(s)")

    if report.file_error:
        return EXIT_FILE_ERROR
    return EXIT_ERRORS if count_errors(findings) else EXIT_OK


def main() -> None:
    """Console entry point."""
    sys.exit(run())


if __name__ == "__main__":
    main()
