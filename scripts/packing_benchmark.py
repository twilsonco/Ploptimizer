#!/usr/bin/env python3
"""Packing-layout benchmark: measure how much plate material a layout wastes.

Plate *count* only says a packing fit; it says nothing about how much of the
sheet the engraver actually visits. For every job spec this script reports, per
plate and per job:

``used``
    Σ label ``w × h`` (sq in) — the labels' own footprint.
``bbox``
    The placed envelope (sq in): the rectangle the layout occupies inside the
    usable area. Material outside it is scrap.
``cov%``
    ``used / material`` — full-sheet utilization, where material is the usable
    area plus the plate's left/top clearance strips (the sheet the shop buys).
``gap``
    ``bbox − used`` — material inside the envelope that no label covers.
``rot``
    ``PackedLabel.rotated`` count: labels the packer turned 90°.
``EMPTY``
    A plate that was opened, consumes a whole sheet, and carries no label.

The script drives the production layout path — :func:`prepare_job` (parse →
expand → resolve), the same pre-passes ``export_per_cutter_plts`` runs before
packing (list markers, line-content reflow, shared h-compression, compression
cutter downsize), the pen map, then
:func:`~plt_optimizer.generate.packing.layout.generate_layout_with_bounds` —
and stops there: no assembly, no PLT/PDF writing. So a spec measures in seconds
and the numbers are the ones the machine will engrave.

Run with::

    python scripts/packing_benchmark.py                    # every examples/job_specs spec, declared layout
    python scripts/packing_benchmark.py examples/job_specs/3-material-test.yaml
    python scripts/packing_benchmark.py --modes all        # rows, columns, rows-simple, columns-simple
    python scripts/packing_benchmark.py --set allow_rotation=false
    python scripts/packing_benchmark.py --json /tmp/base.json

``--set`` applies a dotted override to the raw YAML before the pipeline sees
it, which is how a cascade knob is measured before (and after) it gets a home
in ``job-config.json``. ``--json`` writes the same metrics machine-readably for
A/B diffs (see ``docs/plans/packing-quality.md``).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

import yaml

# Repo root (one level above scripts/) so the pipeline imports and every
# repo-root-relative default (tools.json, job-config.json) work regardless of
# the current working directory.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# NOTE: the sweep's log silencing lives in quiet_logging() (called from main()),
# NOT here. Silencing the root logger at import time is a global side effect that
# leaks into any process importing this module -- tests included, where it silently
# swallows every caplog record below ERROR.

from plt_optimizer.generate.cutter_downsize import apply_compression_cutter_downsize
from plt_optimizer.generate.h_compress import apply_global_h_compress
from plt_optimizer.generate.job_config import JobConfig, load_job_config
from plt_optimizer.generate.job_prep import (
    PreparedJob,
    default_plate_clearance,
    default_plate_size,
    prepare_job,
)
from plt_optimizer.generate.line_content import apply_line_content_reflow
from plt_optimizer.generate.list_layout import apply_list_marker_layout
from plt_optimizer.generate.packing.layout import PackedPlate, generate_layout_with_bounds
from plt_optimizer.generate.relief.export import relief_pen_layers
from plt_optimizer.generate.resolution import build_cutter_pen_map
from plt_optimizer.generate.schema import LayoutMode

#: Every layout mode ``--modes all`` sweeps (the :class:`LayoutMode` values).
SWEEP_MODES: tuple[str, ...] = tuple(mode.value for mode in LayoutMode)

DEFAULT_SPECS_DIR = REPO_ROOT / "examples" / "job_specs"

#: Prefix of the throwaway YAML a ``--set`` override is written to (in the
#: spec's own directory, so relative font/replacement paths keep resolving).
OVERRIDE_TMP_SUFFIX = ".packing-benchmark.tmp.yaml"


@dataclass(frozen=True)
class PlateMetrics:
    """One plate's utilization, in inches / square inches."""

    plate_id: str
    material: str
    count: int
    used_sq_in: float
    bbox_w_in: float
    bbox_h_in: float
    bbox_sq_in: float
    material_sq_in: float
    coverage_pct: float
    gap_sq_in: float
    rotated: int

    @property
    def is_empty(self) -> bool:
        """True when the plate was opened and carries no label."""
        return self.count == 0


@dataclass(frozen=True)
class JobMetrics:
    """One (spec, layout mode) measurement: per-plate rows plus job totals."""

    spec: str
    job_name: str
    mode: str
    plate_w_in: float
    plate_h_in: float
    sheet_sq_in: float
    plates: tuple[PlateMetrics, ...]
    labels: int
    empty_plates: int
    used_sq_in: float
    bbox_sq_in: float
    gap_sq_in: float
    coverage_pct: float
    rotated: int

    def to_dict(self) -> dict[str, Any]:
        """Machine-readable form for ``--json`` A/B diffs."""
        payload = asdict(self)
        payload["plates"] = [asdict(plate) for plate in self.plates]
        return payload


def apply_overrides(raw: dict[str, Any], overrides: Sequence[str]) -> dict[str, Any]:
    """Apply ``dotted.key=value`` overrides to a raw spec mapping, in order.

    Args:
        raw: Parsed job-spec mapping (mutated in place and returned).
        overrides: ``"key.path=value"`` strings. A missing mapping key is
            created, so a cascade knob can be measured before it has a home in
            the schema (``--set job.rotation_preference=minimize`` on a spec
            that never mentions it). Numeric segments index lists, so
            ``plates.0.plate_rotation=0`` edits the first plate; a list index
            must already exist. A later override of the same key wins.

    Returns:
        The same mapping that was mutated, for chaining.

    Raises:
        ValueError: An override has no ``=``, has an empty key segment, indexes
            a list out of range, or descends into something that is neither a
            mapping nor a list.
    """
    for item in overrides:
        key, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"--set expects key=value, got {item!r}")
        segments = key.split(".")
        if not all(segments):
            raise ValueError(f"--set got an empty key segment: {item!r}")
        node: Any = raw
        for segment in segments[:-1]:
            node = _descend(node, segment, item)
        _assign(node, segments[-1], _coerce(value), item)
    return raw


def _descend(node: Any, segment: str, source: str) -> Any:
    """Read (creating if absent) one intermediate path segment.

    Raises:
        ValueError: A list index is out of range, or the node is neither a
            mapping nor a list.
    """
    if isinstance(node, dict):
        child = node.setdefault(segment, {})
        if not isinstance(child, (dict, list)):
            raise ValueError(
                f"--set {source!r}: cannot descend into {segment!r} ({type(child).__name__})"
            )
        return child
    if isinstance(node, list):
        position = _index(segment, source)
        if position >= len(node):
            raise ValueError(f"--set {source!r}: index {position} out of range")
        return node[position]
    raise ValueError(f"--set {source!r}: cannot descend into {type(node).__name__}")


def _assign(node: Any, segment: str, value: Any, source: str) -> None:
    """Write one leaf path segment of a spec mapping or list.

    Raises:
        ValueError: The node is neither a mapping nor a list, or a list index
            is out of range.
    """
    if isinstance(node, dict):
        node[segment] = value
        return
    if isinstance(node, list):
        position = _index(segment, source)
        if position >= len(node):
            raise ValueError(f"--set {source!r}: index {position} out of range")
        node[position] = value
        return
    raise ValueError(f"--set {source!r}: cannot assign into {type(node).__name__}")


def _index(segment: str, source: str) -> int:
    """Parse a list-index path segment.

    Raises:
        ValueError: The segment is not an integer.
    """
    try:
        return int(segment)
    except ValueError as exc:
        raise ValueError(f"--set {source!r}: {segment!r} is not a list index") from exc


def _coerce(value: str) -> Any:
    """Parse a ``--set`` value into bool / None / int / float / list / string."""
    stripped = value.strip()
    lowered = stripped.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        pass
    if stripped.startswith("[") and stripped.endswith("]"):
        return [
            _coerce(part) for part in stripped[1:-1].split(",") if part.strip()
        ]  # a trailing comma is allowed
    return stripped


def load_tool_inventory(path: Path) -> tuple[list[float], float | None]:
    """Read ``tools.json`` the way the CLI does.

    Args:
        path: Path to ``tools.json``. A missing file selects ideal cutters
            (empty inventory), exactly like the CLI's ``--tools`` default.

    Returns:
        ``(available cutter diameters in inches, boundary/hole cutter size in
        inches or None)``.
    """
    if not path.is_file():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name}: unreadable cutter inventory: {exc}") from exc
    inventory = list(data.get("available_cutters", []))
    return inventory, data.get("boundary_hole_cutter_size")


def prepare(
    path: Path,
    overrides: Sequence[str] = (),
    *,
    config_path: Path = REPO_ROOT / "job-config.json",
    tools_path: Path = REPO_ROOT / "tools.json",
) -> PreparedJob:
    """Resolve a spec YAML exactly like ``plt-optimizer generate`` does.

    Args:
        path: Job-spec YAML. A jobset file (root key ``jobs``) is rejected —
            measure its members, the set's own pool is a batch-level question.
        overrides: ``--set`` strings, applied to the raw mapping before the
            pipeline sees it (through a throwaway YAML in the same directory).
        config_path: Shop ``job-config.json``.
        tools_path: Cutter inventory ``tools.json``.
    Returns:
        The prepared job (parsed, expanded, geometry-resolved) the packer eats.

    Raises:
        ValueError: The root YAML is not a mapping, is a jobset, or an override
            is malformed.
        plt_optimizer.generate.job_prep.JobPreparationError: Parse/resolve failed.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: root is not a mapping")
    if "jobs" in raw:
        raise ValueError(f"{path.name}: jobset file — measure its member specs directly")
    inventory, boundary_hole_cutter = load_tool_inventory(tools_path)
    job_config: JobConfig | None = load_job_config(config_path)
    if not overrides:
        return prepare_job(
            path,
            job_config=job_config,
            job_config_path=config_path,
            inventory=inventory,
            boundary_hole_cutter=boundary_hole_cutter,
        )
    apply_overrides(raw, overrides)
    tmp = path.with_name(f"{path.stem}{OVERRIDE_TMP_SUFFIX}")
    tmp.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    try:
        return prepare_job(
            tmp,
            job_config=job_config,
            job_config_path=config_path,
            inventory=inventory,
            boundary_hole_cutter=boundary_hole_cutter,
        )
    finally:
        tmp.unlink(missing_ok=True)


def pack(
    prepared: PreparedJob,
    mode: str | None = None,
    *,
    config_path: Path = REPO_ROOT / "job-config.json",
    tools_path: Path = REPO_ROOT / "tools.json",
) -> list[PackedPlate]:
    """Run the production pre-passes and pack, in ``mode`` (optional override).

    Args:
        prepared: A prepared job from :func:`prepare`.
        mode: A :class:`~plt_optimizer.generate.schema.LayoutMode` value, or
            ``None`` to keep the spec's declared layout. When given, it is
            applied to the job **and** to every plate that declared its own, so
            a sweep compares like with like.
        config_path: Shop config supplying the unbounded default plate size.
        tools_path: Cutter inventory for the compression cutter downsize.

    Returns:
        The packed plates, packing order (plate numbers ascending).
    """
    job = prepared.job
    if mode is not None:
        job.layout = LayoutMode(mode)
        for plate in job.plates or []:
            if plate.layout is not None:
                plate.layout = LayoutMode(mode)

    # The same pre-passes export_per_cutter_plts runs before it packs, in the
    # same order: they change label sizes, so packing on unprepared labels
    # would measure a layout the machine never gets.
    labels = apply_list_marker_layout(prepared.resolved_labels)
    labels = apply_line_content_reflow(labels)
    labels = apply_global_h_compress(labels)
    inventory, _hole = load_tool_inventory(tools_path)
    labels = apply_compression_cutter_downsize(labels, inventory)
    pen_map = build_cutter_pen_map(
        labels,
        extra_layers=relief_pen_layers(labels, inventory),
    )
    packed, _rendered = generate_layout_with_bounds(
        labels,
        job.plates,
        pen_map=pen_map,
        allow_rotation=job.allow_rotation,
        layout=job.layout,
        default_plate_size=_config_plate_size(config_path),
        default_plate_clearance=default_plate_clearance(job),
    )
    return packed


_CONFIG_PLATE_SIZE_CACHE: dict[str, tuple[float, float] | None] = {}


def _config_plate_size(
    config_path: Path = REPO_ROOT / "job-config.json",
) -> tuple[float, float] | None:
    """Default plate size for unbounded jobs (from ``job-config.json``)."""
    key = str(config_path)
    if key not in _CONFIG_PLATE_SIZE_CACHE:
        _CONFIG_PLATE_SIZE_CACHE[key] = default_plate_size(load_job_config(config_path))
    return _CONFIG_PLATE_SIZE_CACHE[key]


def measure_plate(plate: PackedPlate) -> PlateMetrics:
    """Measure one packed plate.

    The plate's ``width``/``height`` are the *usable* area; the left/top
    clearance strips are material the shop buys and never engraves, so they are
    part of ``material_sq_in`` (the sheet) and never part of the envelope.
    """
    labels = list(plate.labels)
    sheet_w = plate.width + plate.left_clearance
    sheet_h = plate.height + plate.top_clearance
    sheet_area = sheet_w * sheet_h
    used = sum(label.width * label.height for label in labels)
    if labels:
        left = min(label.x for label in labels)
        right = max(label.x + label.width for label in labels)
        bottom = min(label.y for label in labels)
        top = max(label.y + label.height for label in labels)
        bbox_w, bbox_h = right - left, top - bottom
    else:
        bbox_w = bbox_h = 0.0
    bbox_area = bbox_w * bbox_h
    gap = bbox_area - used
    if abs(gap) < 1e-9:
        # Labels that tile the envelope exactly must not print as "-0.00".
        gap = 0.0
    return PlateMetrics(
        plate_id=plate.plate_id,
        material=plate.material or "(unset)",
        count=len(labels),
        used_sq_in=used,
        bbox_w_in=bbox_w,
        bbox_h_in=bbox_h,
        bbox_sq_in=bbox_area,
        material_sq_in=sheet_area,
        coverage_pct=used / sheet_area * 100.0 if sheet_area else 0.0,
        gap_sq_in=gap,
        rotated=sum(1 for label in labels if label.rotated),
    )


def measure_job(
    prepared: PreparedJob,
    mode: str | None,
    *,
    spec_name: str = "",
    config_path: Path = REPO_ROOT / "job-config.json",
    tools_path: Path = REPO_ROOT / "tools.json",
) -> JobMetrics:
    """Pack ``prepared`` and measure every plate it produced.

    Args:
        prepared: A prepared job from :func:`prepare`.
        mode: Layout mode override (``None`` = the spec's declared layout).
        spec_name: Label for the report (the YAML file name, typically).
        config_path: Shop config supplying the unbounded default plate size.
        tools_path: Cutter inventory for the compression cutter downsize.

    Returns:
        Per-plate metrics plus job totals, plates in packing order.
    """
    packed = pack(prepared, mode, config_path=config_path, tools_path=tools_path)
    rows = [measure_plate(plate) for plate in packed]
    used = sum(row.used_sq_in for row in rows)
    bbox = sum(row.bbox_sq_in for row in rows)
    sheets = sum(row.material_sq_in for row in rows)
    first = packed[0] if packed else None
    return JobMetrics(
        spec=spec_name,
        job_name=prepared.name,
        mode=(mode or prepared.job.layout.value),
        plate_w_in=(first.width + first.left_clearance) if first else 0.0,
        plate_h_in=(first.height + first.top_clearance) if first else 0.0,
        sheet_sq_in=sheets,
        plates=tuple(rows),
        labels=sum(row.count for row in rows),
        empty_plates=sum(1 for row in rows if row.is_empty),
        used_sq_in=used,
        bbox_sq_in=bbox,
        gap_sq_in=bbox - used,
        coverage_pct=used / sheets * 100.0 if sheets else 0.0,
        rotated=sum(row.rotated for row in rows),
    )


def format_job(metrics: JobMetrics) -> str:
    """Render one job's metrics as an aligned report block."""
    lines = [
        f"{metrics.spec}  job={metrics.job_name!r}  mode={metrics.mode}"
        f"  sheet={metrics.plate_w_in:g}x{metrics.plate_h_in:g}in",
        f"  {'plate':<24} {'material':<20} {'n':>4} {'used':>8} "
        f"{'bbox':>16} {'sheet':>7} {'cov%':>6} {'gap':>7} {'rot':>4}",
    ]
    for row in metrics.plates:
        flag = "  EMPTY" if row.is_empty else ""
        lines.append(
            f"  {row.plate_id:<24} {row.material[:20]:<20} {row.count:>4} "
            f"{row.used_sq_in:>8.2f} "
            f"{row.bbox_w_in:>6.2f}x{row.bbox_h_in:<6.2f}{row.bbox_sq_in:>7.1f} "
            f"{row.material_sq_in:>7.1f} {row.coverage_pct:>6.1f} "
            f"{row.gap_sq_in:>7.2f} {row.rotated:>4}{flag}"
        )
    materials = len({row.material for row in metrics.plates})
    lines.append(
        f"  {'TOTAL':<24} {materials:>3} material(s)  labels={metrics.labels}"
        f"  plates={len(metrics.plates)}  empty={metrics.empty_plates}"
    )
    lines.append(
        f"  {'':<24} used={metrics.used_sq_in:.2f} in²  bbox={metrics.bbox_sq_in:.2f} in²"
        f"  gap={metrics.gap_sq_in:.2f} in²  coverage={metrics.coverage_pct:.1f}%"
        f"  rotated={metrics.rotated}"
    )
    return "\n".join(lines)


def collect_specs(paths: Sequence[Path]) -> list[Path]:
    """Expand files/directories into a de-duplicated spec file list.

    A directory contributes its top-level ``*.yaml`` / ``*.yml`` files, sorted;
    one with no spec YAMLs (a job-set directory, whose members live beside its
    set file) is skipped with a warning. Non-YAML files are skipped too.
    """
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            candidates = sorted(
                item
                for item in path.iterdir()
                if item.suffix.lower() in {".yaml", ".yml"} and item.is_file()
            )
            if not candidates:
                print(f"  skip {path.name}: no top-level spec YAMLs", file=sys.stderr)
            found.extend(candidates)
        elif path.suffix.lower() in {".yaml", ".yml"}:
            found.append(path)
        else:
            print(f"  skip {path}: not a YAML spec", file=sys.stderr)
    return list(dict.fromkeys(found))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="packing_benchmark.py",
        description="Measure plate utilization (coverage, envelope, waste, rotations).",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Job-spec YAMLs and/or directories (default: examples/job_specs).",
    )
    parser.add_argument(
        "--modes",
        default="declared",
        help="'declared' (each spec's own layout), 'all', or a comma list of: "
        + ", ".join(SWEEP_MODES)
        + ".",
    )
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Dotted override applied to the raw YAML (repeatable), e.g. "
        "--set allow_rotation=false --set plates.0.plate_rotation=0.",
    )
    parser.add_argument("--json", dest="json_path", type=Path, help="Write the metrics JSON here.")
    parser.add_argument(
        "--config",
        type=Path,
        default=REPO_ROOT / "job-config.json",
        help="Shop job-config.json (default: repo root).",
    )
    parser.add_argument(
        "--tools",
        type=Path,
        default=REPO_ROOT / "tools.json",
        help="Cutter inventory tools.json (default: repo root).",
    )
    return parser.parse_args(argv)


def quiet_logging() -> None:
    """Silence the pipeline's per-label typography warnings for a clean sweep.

    A layout run logs a WARNING per compressed line, per cutter downsize and
    per collision retry (thousands of lines), and ``job_prep`` logs INFO stage
    timers. The pipeline installs its own handlers/levels when the first
    pipeline call runs, so handler filters would be attached too late —
    :func:`logging.disable` is a global threshold below the whole logging
    system, which is what a measurement script wants: only real errors print.
    """
    logging.disable(logging.ERROR)
    logging.getLogger().setLevel(logging.ERROR)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the benchmark over every selected spec and mode."""
    quiet_logging()
    args = parse_args(argv)
    if args.modes == "all":
        modes: tuple[str, ...] = SWEEP_MODES
    elif args.modes == "declared":
        modes = ()
    else:
        modes = tuple(mode.strip() for mode in args.modes.split(",") if mode.strip())
        unknown = [mode for mode in modes if mode not in SWEEP_MODES]
        if unknown:
            print(
                f"Unknown layout mode(s) {unknown}; choose from {list(SWEEP_MODES)}.",
                file=sys.stderr,
            )
            return 2

    paths = collect_specs(args.paths or [DEFAULT_SPECS_DIR])
    if not paths:
        print("No job specs found.", file=sys.stderr)
        return 1

    results: list[JobMetrics] = []
    failures = 0
    for path in paths:
        try:
            prepared = prepare(path, args.overrides, config_path=args.config, tools_path=args.tools)
        except Exception as exc:  # one bad spec must not stop the sweep
            failures += 1
            print(f"{path.name}: PREP FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        for mode in modes or (None,):
            try:
                metrics = measure_job(
                    prepared,
                    mode,
                    spec_name=path.name,
                    config_path=args.config,
                    tools_path=args.tools,
                )
            except Exception as exc:
                failures += 1
                label = mode or prepared.job.layout.value
                print(
                    f"{path.name} [{label}]: PACK FAILED {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                continue
            results.append(metrics)
            print(format_job(metrics))
            sys.stdout.flush()

    if args.json_path is not None:
        payload = {
            "schema_version": 1,
            "modes": list(modes) or ["declared"],
            "overrides": list(args.overrides),
            "jobs": [metrics.to_dict() for metrics in results],
        }
        args.json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nwrote {args.json_path}", file=sys.stderr)
    print(
        f"\n{len(results)} measurement(s) over {len(paths)} spec(s); {failures} failure(s).",
        file=sys.stderr,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
