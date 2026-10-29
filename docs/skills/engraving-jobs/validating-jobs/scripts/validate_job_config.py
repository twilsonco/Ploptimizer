#!/usr/bin/env python3
"""Validate a ``job-config.json`` shop-defaults file.

The generate pipeline's config gate is
:func:`plt_optimizer.generate.job_config.load_job_config`, which enforces the
:class:`~plt_optimizer.generate.job_config.JobDefaults` model (unknown keys,
ranges, enums, the ``tool_options`` metadata shape, font existence). This
script is a thin CLI over that gate plus the config-only advisory checks the
pipeline cannot surface until a specific job spec fails:

- a required-when-unconfigured field the config does not supply (every spec
  must then declare it, or parsing aborts),
- a missing default plate size (any spec without ``plates:`` aborts),
- ``tool_options`` entries missing the ``name`` / ``description`` / ``units``
  metadata the documentation generators render.

Run with::

    uv run python scripts/validate_job_config.py
    uv run python scripts/validate_job_config.py --config path/to/config.json
    uv run python scripts/validate_job_config.py --json --strict

This file lives in the repo ``scripts/`` directory and is mirrored byte-for-byte
into the ``validating-jobs`` Hermes skill (``validating-jobs/scripts/``), so it
locates the pipeline itself instead of assuming a fixed depth below a repo root:
an importable ``plt_optimizer`` (installed package, ``uv run`` in a checkout,
``PYTHONPATH``) is used as-is, otherwise the nearest checkout root above this
file is added to ``sys.path``. The ``--config`` default is cwd-relative, exactly
like ``plt-optimizer generate``, so run it beside the files it validates (or
pass ``--config``).

Exit codes: ``0`` clean (warnings allowed) · ``1`` one or more errors ·
``2`` the file could not be read, or the pipeline could not be located.

Stdlib + ``plt_optimizer`` only: ``jsonschema`` is a dev extra and is not
installed on the Windows 7 plotter PC this config drives.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
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
    "       uv run python scripts/validate_job_config.py ...\n"
    "  2. anywhere else (e.g. an installed skill): install the package\n"
    "       uv tool install plt-optimizer   (or: pip install plt-optimizer)\n"
    "     then run it beside the job-config.json it validates, or pass --config."
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

from plt_optimizer.generate.job_config import (  # noqa: E402
    REQUIRED_WHEN_UNCONFIGURED,
    TOOL_OPTION_DEFAULT_LAYERS,
    JobConfig,
    JobConfigError,
    JobDefaults,
    load_job_config,
)

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

#: Advisory ``tool_options`` metadata the docs generators render. A missing
#: key never breaks a run; it only leaves the generated tables sparse.
ADVISORY_OPTION_FIELDS: tuple[str, ...] = ("name", "description", "units")


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


def check_required_fields(defaults: JobDefaults) -> list[Finding]:
    """Warn about required-when-unconfigured fields the config omits.

    With a config in play, each field in
    :data:`~plt_optimizer.generate.job_config.REQUIRED_WHEN_UNCONFIGURED`
    must come from the config or from every job spec. A config that omits one
    is legal, so this is advisory: it tells the shop operator which fields
    every spec will have to carry.

    Args:
        defaults: The validated config defaults.

    Returns:
        One warning per omitted field.
    """
    findings: list[Finding] = []
    for name in sorted(REQUIRED_WHEN_UNCONFIGURED):
        if getattr(defaults, name) is None:
            findings.append(
                _warning(
                    f"{name} is not configured: every job spec must declare it "
                    f"(job level, or on every label) or parsing fails"
                )
            )
    return findings


def check_plate_size(defaults: JobDefaults) -> list[Finding]:
    """Warn when the config supplies no default plate size.

    A job spec that omits ``plates:`` packs in unbounded mode onto
    config-sized default sheets; without both dimensions such a spec aborts.

    Args:
        defaults: The validated config defaults.

    Returns:
        One warning when a dimension is missing (none when both are set).
    """
    missing = [name for name in ("plate_width", "plate_height") if getattr(defaults, name) is None]
    if not missing:
        return []
    return [
        _warning(
            f"{' and '.join(missing)} not configured: a job spec without "
            f"`plates:` has no default sheet to pack onto and will fail"
        )
    ]


def check_tool_options_advisory(defaults: JobDefaults) -> list[Finding]:
    """Warn about ``tool_options`` entries missing advisory documentation keys.

    The structural gate (``command`` / ``type`` / ``default`` / bounds) lives
    in :class:`JobDefaults`; this only reports the cosmetic keys the docs
    generators render into the tool-options tables.

    Args:
        defaults: The validated config defaults.

    Returns:
        One warning per entry missing advisory metadata.
    """
    metadata = defaults.tool_options
    if not isinstance(metadata, dict):
        return []
    findings: list[Finding] = []
    for key in sorted(metadata, key=str):
        entry = metadata[key]
        if not isinstance(entry, dict):
            continue  # rejected by the structural gate
        missing = [name for name in ADVISORY_OPTION_FIELDS if not entry.get(name)]
        if missing:
            findings.append(
                _warning(
                    f"tool_options.{key}: no {', '.join(missing)} "
                    f"(documentation tables render sparse)"
                )
            )
    return findings


def check_tool_options_defaults(defaults: JobDefaults) -> list[Finding]:
    """Warn about dual defaults that omit a layer.

    A dual default whose value for one layer is explicitly ``null`` emits no
    header for that PLT type. That is a legitimate "omit" choice, so it is
    reported as a warning rather than an error.

    Args:
        defaults: The validated config defaults.

    Returns:
        One warning per layer explicitly set to null.
    """
    metadata = defaults.tool_options
    if not isinstance(metadata, dict):
        return []
    findings: list[Finding] = []
    for key in sorted(metadata, key=str):
        entry = metadata[key]
        if not isinstance(entry, dict):
            continue
        default = entry.get("default")
        if not isinstance(default, dict):
            continue
        for layer in TOOL_OPTION_DEFAULT_LAYERS:
            if layer in default and default[layer] is None:
                findings.append(
                    _warning(
                        f"tool_options.{key}.default.{layer} is null: no header "
                        f"is emitted for {layer} PLTs unless a spec overrides it"
                    )
                )
    return findings


@dataclass(frozen=True)
class ConfigReport:
    """Result of validating one job-config file.

    Attributes:
        findings: Every error/warning discovered.
        config: The loaded config, or ``None`` when it could not be built.
        file_error: True when the file itself was absent, unreadable, or not
            valid JSON (exit 2), as opposed to failing validation (exit 1).
    """

    findings: list[Finding]
    config: Optional[JobConfig]
    file_error: bool = False


def validate_config(path: Path) -> ConfigReport:
    """Run every check against one job-config file.

    Args:
        path: Path to the ``job-config.json`` to validate.

    Returns:
        The report; ``config`` is ``None`` when the file is absent,
        unreadable, not JSON, or fails validation. A file-level failure
        (absent / unreadable / not JSON) sets ``file_error``.
    """
    if not path.is_file():
        return ConfigReport([_error("no such file")], None, file_error=True)

    try:
        config = load_job_config(path)
    except JobConfigError as exc:
        # load_job_config chains the underlying cause: OSError for an
        # unreadable file, JSONDecodeError for malformed JSON, ValidationError
        # for content that fails the model. Only the first two are file-level.
        file_error = isinstance(exc.__cause__, (OSError, json.JSONDecodeError))
        return ConfigReport([_error(str(exc))], None, file_error=file_error)

    if config is None:  # pragma: no cover - a path is always supplied here
        return ConfigReport([_error("config layer disabled")], None, file_error=True)

    findings: list[Finding] = []
    defaults = config.defaults
    findings.extend(check_required_fields(defaults))
    findings.extend(check_plate_size(defaults))
    findings.extend(check_tool_options_advisory(defaults))
    findings.extend(check_tool_options_defaults(defaults))
    return ConfigReport(findings, config)


def count_errors(findings: Sequence[Finding], strict: bool = False) -> int:
    """Count findings that should fail the run.

    Args:
        findings: The findings to tally.
        strict: When True, warnings count as errors too.

    Returns:
        The number of failing findings.
    """
    if strict:
        return len(findings)
    return sum(1 for finding in findings if finding.severity == SEVERITY_ERROR)


def format_text(path: Path, findings: Sequence[Finding]) -> str:
    """Render findings as human-readable lines.

    Args:
        path: The config path being reported on.
        findings: The findings to render.

    Returns:
        The report text (no trailing newline).
    """
    lines: list[str] = []
    for finding in findings:
        lines.append(f"{finding.severity.upper():7s} {path}: {finding.message}")
    if not lines:
        lines.append(f"OK {path}: job config is valid")
    return "\n".join(lines)


def format_json(path: Path, findings: Sequence[Finding], strict: bool) -> str:
    """Render findings as a JSON document.

    Args:
        path: The config path being reported on.
        findings: The findings to render.
        strict: Whether warnings were treated as errors.

    Returns:
        A JSON string.
    """
    payload: dict[str, Any] = {
        "path": str(path),
        "ok": count_errors(findings, strict) == 0,
        "strict": strict,
        "errors": sum(1 for f in findings if f.severity == SEVERITY_ERROR),
        "warnings": sum(1 for f in findings if f.severity == SEVERITY_WARNING),
        "findings": [finding.as_dict() for finding in findings],
    }
    return json.dumps(payload, indent=2)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="validate_job_config.py",
        description="Validate a job-config.json shop-defaults file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes: 0 clean (warnings allowed) | 1 errors | "
            "2 file unreadable, or the pipeline could not be located"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        # Cwd-relative, exactly like `generate --job-config`: run the helper beside
        # the file it validates (the repo root, a per-engraver config directory,
        # an installed skill's working directory) or pass an explicit path.
        default=Path("job-config.json"),
        help="Path to the job defaults JSON (default: job-config.json in the CWD).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON report instead of text.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors (fails on advisory findings too).",
    )
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        The process exit code.
    """
    args = build_parser().parse_args(argv)
    path: Path = args.config
    report = validate_config(path)
    findings = report.findings

    if args.json:
        print(format_json(path, findings, args.strict))
    else:
        print(format_text(path, findings))
        n_errors = sum(1 for f in findings if f.severity == SEVERITY_ERROR)
        n_warnings = sum(1 for f in findings if f.severity == SEVERITY_WARNING)
        if n_errors or n_warnings:
            print(f"{n_errors} error(s), {n_warnings} warning(s)")

    if report.file_error:
        return EXIT_FILE_ERROR
    return EXIT_ERRORS if count_errors(findings, args.strict) else EXIT_OK


def main() -> None:
    """Console entry point."""
    sys.exit(run())


if __name__ == "__main__":
    main()
