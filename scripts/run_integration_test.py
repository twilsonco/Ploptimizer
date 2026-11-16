#!/usr/bin/env python3
"""End-to-End Integration Test Runner.

This script executes the full pipeline from YAML ingestion to PLT export,
with intermediate state dumps for verification. Validates:

- Hierarchical resolution and inheritance cascade
- Cutter compensation and 3x tolerance logic
- Bin packing and multi-plate allocation
- Vectorization and PLT export
- The ``generate`` pipeline's report surface (``generation_report/report.json``
  + ``summary.txt``), written on success **and** on failure like the CLI

Run with: python scripts/run_integration_test.py
Or target a specific spec / every spec in a directory:
    python scripts/run_integration_test.py tests_deps/complex_test_job.yaml
    python scripts/run_integration_test.py tests_deps
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path

# Repo root (one level above scripts/) so the pipeline imports and every
# repo-root-relative default (tests_deps/, tools.json, job-config.json,
# test_output/) work regardless of the current working directory.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow running as a plain script
    sys.path.insert(0, str(REPO_ROOT))

# Setup logging for visibility
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Opt-in switch for the color-coded *_default.pdf diagnostic plots (rapid
# travel visualization) in Phase 4. Off by default: the simple-outline
# previews written by the export step are the standard artifacts, and the
# color plots are slow to render. Flip to True (or export_per_cutter_plts'
# default_plots= kwarg / the CLI's --default-plots flag) to produce them.
GENERATE_DEFAULT_PLOTS = True

# Import pipeline components
from plt_optimizer.core.units import units_to_inches
from plt_optimizer.generate.generation_report import (
    ReportCapture,
    build_report,
    format_summary,
    report_dir_for,
    utc_now_iso,
    write_report,
)
from plt_optimizer.generate.job_config import JobConfig, load_job_config
from plt_optimizer.generate.label_renderer import measure_line_vertical_extents
from plt_optimizer.generate.layout_report import format_layout_report
from plt_optimizer.generate.packing.layout import PackedPlate, generate_layout
from plt_optimizer.generate.resolution import (
    ResolvedLabel,
    memoize_extents_probe,
    resolve_job_spec,
)
from plt_optimizer.generate.schema import (
    DEFAULT_LAYOUT_MODE,
    JobSpec,
    LayoutMode,
    PlateSpec,
    parse_yaml,
)
from plt_optimizer.generate.substitution import expand_job_spec
from plt_optimizer.generate.vectorize import (
    PerCutterExport,
    export_per_cutter_plts,
    write_default_plots,
)
from plt_optimizer.utils.logging import TextLogger
from plt_optimizer.utils.stage_timing import StageTimings, stage_timer


def load_tool_inventory(inventory_path: Path) -> tuple[list[float], float | None]:
    """Load available cutter diameters from tools.json.

    Args:
        inventory_path: Path to tools.json.

    Returns:
        Tuple of (available cutter diameters in inches, requested
        boundary/hole cutter size in inches or ``None`` when the key is
        absent).

    Raises:
        FileNotFoundError: If inventory_path does not exist.
        json.JSONDecodeError: If JSON is malformed.
    """
    with open(inventory_path) as f:
        data = json.load(f)
    inventory = data.get("available_cutters", [])
    boundary_hole_cutter = data.get("boundary_hole_cutter_size")
    logger.info(f"Loaded cutter inventory: {inventory}")
    logger.info(f"Loaded boundary/hole cutter size: {boundary_hole_cutter}")
    return inventory, boundary_hole_cutter


def print_separator(title: str) -> None:
    """Print a formatted section separator."""
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80 + "\n")


def _resolve_output_dir(job_yaml: Path, spec_override: Path | None) -> Path:
    """Resolve the absolute output directory for a run.

    Every artifact of a run (``plt/``, ``pdf/`` and the
    ``generation_report/`` directory) lives under this directory, so both
    the success path and the failure path derive it the same way. With a
    spec override the run is namespaced by the spec's stem
    (``test_output/integration_test/<spec-stem>/``); the default spec keeps
    the historical ``test_output/integration_test/``.

    Args:
        job_yaml: The job spec this run is processing.
        spec_override: The spec path the user passed (``None`` = the
            built-in default spec).

    Returns:
        The absolute output directory.
    """
    if spec_override is None:
        return REPO_ROOT / "test_output" / "integration_test"
    return REPO_ROOT / "test_output" / "integration_test" / job_yaml.stem


def _display_path(path: Path) -> str:
    """Render an artifact path for the console.

    Repo-relative paths read better in the run log, so they are shortened;
    a path outside the repo (an explicit output directory, a scratch tree)
    has no meaningful relative form and is printed absolute.

    Args:
        path: The artifact path to render.

    Returns:
        The path as a string, relative to the repo root when it is inside.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _report_tools(
    inventory: list[float],
    boundary_hole_cutter: float | None,
    job_config: JobConfig | None,
) -> dict[str, object]:
    """Describe the cutter inventory for the report's ``job.tools`` section.

    Mirrors the mapping the ``generate`` CLI builds, so a report reader
    cannot tell an integration run from a CLI run by shape.

    Args:
        inventory: Available cutter diameters (inches).
        boundary_hole_cutter: Requested boundary/hole cutter size, or
            ``None`` when unset.
        job_config: Loaded shop defaults, or ``None``.

    Returns:
        A JSON-ready mapping.
    """
    defaults = job_config.defaults if job_config is not None else None
    return {
        "available_cutters": list(inventory),
        "boundary_hole_cutter_size": boundary_hole_cutter,
        "cutter_buffer_text": defaults.cutter_buffer_text if defaults is not None else None,
        "cutter_buffer_routing": defaults.cutter_buffer_routing if defaults is not None else None,
    }


def _report_options() -> dict[str, object]:
    """Describe this run's switches for the report's ``job.options`` section.

    Returns:
        A JSON-ready mapping, carrying a ``source`` marker so a reader can
        tell an integration run from a ``generate`` CLI run.
    """
    return {
        "source": "scripts/run_integration_test.py",
        "optimize": True,
        "plots": True,
        "default_plots": GENERATE_DEFAULT_PLOTS,
        "fast_mode": False,
    }


def _job_identity(job: JobSpec | None, job_yaml: Path) -> tuple[str, str, int]:
    """Extract the report's job identity, tolerating a pre-parse failure.

    Args:
        job: The parsed job spec, or ``None`` when the run failed before
            parsing finished (the spec stem is then the identity).
        job_yaml: The job spec path being processed.

    Returns:
        Tuple of (job_name, filesystem-safe job_id, declared plate count).
    """
    from plt_optimizer.cli.generate import _sanitize_job_id

    if job is None:
        name = job_yaml.stem
        return name, _sanitize_job_id(name), 0
    plate_count = len(job.plates) if job.plates is not None else 0
    return job.job_name, _sanitize_job_id(job.job_name), plate_count


# ============================================================================
# PHASE 1: TEST DATA PREPARATION
# ============================================================================
def phase_1_data_prep(
    job_yaml_override: Path | None = None,
) -> tuple[Path, Path, list[float], float | None, Path]:
    """Phase 1: Load test data and inventory.

    Args:
        job_yaml_override: Optional path to an alternative job spec YAML.
            If None, defaults to ``tests_deps/test123_spec.yaml``.

    Returns:
        Tuple of (job_yaml_path, tools_json_path, inventory,
        boundary_hole_cutter_size, job_config_path).
    """
    print_separator("PHASE 1: TEST DATA PREPARATION")

    workspace = REPO_ROOT
    job_yaml = job_yaml_override or workspace / "tests_deps" / "test123_spec.yaml"
    if not job_yaml.is_absolute():
        job_yaml = workspace / job_yaml
    tools_json = workspace / "tools.json"
    job_config_json = workspace / "job-config.json"

    if not job_yaml.exists():
        raise FileNotFoundError(f"Test job YAML not found: {job_yaml}")
    if not tools_json.exists():
        raise FileNotFoundError(f"Tools inventory not found: {tools_json}")

    inventory, boundary_hole_cutter = load_tool_inventory(tools_json)
    if job_config_json.exists():
        logger.info(f"Loaded job defaults from: {job_config_json}")
    else:
        logger.info("No job-config.json found; job spec must declare required fields.")
    logger.info(f"Loaded job spec from: {job_yaml}")
    logger.info(f"Loaded tool inventory from: {tools_json}")

    return job_yaml, tools_json, inventory, boundary_hole_cutter, job_config_json


# ============================================================================
# PHASE 2: PIPELINE EXECUTION
# ============================================================================
def phase_2_resolution_and_layout(
    job_yaml: Path,
    inventory: list[float],
    boundary_hole_cutter_size: float | None = None,
    job_config_path: Path | None = None,
    stage_timings: StageTimings | None = None,
) -> tuple[
    list[ResolvedLabel],
    list[PackedPlate],
    list[PlateSpec] | None,
    str,
    str,
    bool,
    LayoutMode,
    tuple[float, float] | None,
    tuple[float, float] | None,
    JobSpec,
    JobConfig | None,
]:
    """Phase 2: Resolution, bin packing, and verification.

    Executes:
    1. Load and parse JobSpec from YAML
    2. Resolve labels with cutter compensation
    3. Bin pack onto physical plates

    Args:
        job_yaml: Path to test job YAML.
        inventory: List of available cutter diameters.
        boundary_hole_cutter_size: Requested cutter size for label
            boundaries and drill holes (feeds the collision stroke floor;
            ``None`` uses the default).
        job_config_path: Optional path to ``job-config.json`` supplying
            top-layer defaults (and the default plate size for unbounded
            packing). ``None`` keeps the all-optional contract.
        stage_timings: Optional per-stage timing collector; the parse,
            resolve and reporting-layout stages are recorded into it so
            Phase 5's report carries them.

    Returns:
        Tuple of (resolved_labels, packed_plates, provided_plates, job_id,
        job_name, allow_rotation, layout, default_plate_size,
        default_plate_clearance, job, job_config).
    """
    print_separator("PHASE 2: PIPELINE EXECUTION")

    job_config: JobConfig | None = load_job_config(job_config_path)
    default_plate_size: tuple[float, float] | None = None
    if job_config is not None:
        defaults = job_config.defaults
        if defaults.plate_width is not None and defaults.plate_height is not None:
            default_plate_size = (defaults.plate_width, defaults.plate_height)

    # =========================================================================
    # Step 1: Parse JobSpec
    # =========================================================================
    logger.info("Step 1: Parsing JobSpec from YAML...")
    with stage_timer("parse", logger, timings=stage_timings):
        job = expand_job_spec(parse_yaml(job_yaml, job_config_path=job_config_path), job_yaml)
    logger.info(f"Parsed job: {job.job_name}")
    logger.info(f"Job-level text_height: {job.text_height}")
    logger.info(f"Job-level margin: {job.margin}")

    # Job-level edge clearances (job spec or job-config.json injection)
    # also shift the unbounded auto-allocated bins.
    from plt_optimizer.cli.generate import _default_plate_clearance

    default_plate_clearance = _default_plate_clearance(job)

    # =========================================================================
    # Step 2: Resolve labels with cutter compensation
    # =========================================================================
    logger.info("Step 2: Resolving labels with cutter compensation...")
    with stage_timer("resolve", logger, timings=stage_timings):
        resolved_labels = resolve_job_spec(
            job,
            available_cutters=inventory,
            boundary_hole_cutter_size=boundary_hole_cutter_size,
            extents_probe=memoize_extents_probe(measure_line_vertical_extents),
        )

    print("\n--- RESOLUTION RESULTS ---\n")
    for label in resolved_labels:
        print(f"Label ID: {label.id}")
        print(f"  Count: {label.count}")
        print(f'  Dimensions: {label.width}" x {label.height}"')
        print(f'  Margin: {label.margin}"')
        print(f"  Content lines: {len(label.content)}")

        for i, line in enumerate(label.content):
            print(f"    Line {i}: '{line.text}'")
            print(f'      Nominal height: {line.nominal_text_height}"')
            cutter_tag = " (explicit)" if line.cutter_size is not None else ""
            print(f'      Cutter diameter: {line.cutter_diameter}"{cutter_tag}')
            print(f'      Toolpath height: {line.toolpath_text_height}"')
        print()

    # =========================================================================
    # Step 3: Bin packing
    # =========================================================================
    logger.info("Step 3: Running bin packing (layout generation)...")
    with stage_timer("layout", logger, timings=stage_timings):
        packed_plates = generate_layout(
            resolved_labels,
            job.plates,
            allow_rotation=job.allow_rotation,
            layout=job.layout,
            default_plate_size=default_plate_size,
            default_plate_clearance=default_plate_clearance,
        )

    print("\n--- BIN PACKING RESULTS ---\n")
    print(f"Fill order: {job.layout.value}")
    print(f"Total plates generated: {len(packed_plates)}")
    for plate in packed_plates:
        print(f"\nPlate {plate.plate_id}:")
        print(f'  Dimensions: {plate.width}" x {plate.height}"')
        print(f'  Clearances: left={plate.left_clearance}", top={plate.top_clearance}"')
        print(f"  Material: {plate.material if plate.material is not None else '(unset)'}")
        print(f"  Labels packed: {len(plate.labels)}")
        for packed_label in plate.labels:
            print(f"    - {packed_label.label_id}")
            print(f"      Position: ({packed_label.x:.2f}, {packed_label.y:.2f})")
            print(f'      Size: {packed_label.width:.2f}" x {packed_label.height:.2f}"')
            print(f"      Rotated: {packed_label.rotated}")

    print_separator("VERIFICATION POINT: Plate Generation")
    print(f"Total plates generated: {len(packed_plates)}")

    # Filesystem-safe job identifier for per-cutter output naming (mirrors
    # the generate CLI's sanitization).
    from plt_optimizer.cli.generate import _sanitize_job_id

    job_id = _sanitize_job_id(job.job_name)

    return (
        resolved_labels,
        packed_plates,
        job.plates,
        job_id,
        job.job_name,
        job.allow_rotation,
        job.layout,
        default_plate_size,
        default_plate_clearance,
        job,
        job_config,
    )


# ============================================================================
# PHASE 3: OPTIMIZATION AND VISUALIZATION
# ============================================================================
def phase_3_vectorization_and_export(
    resolved_labels: list[ResolvedLabel],
    provided_plates: list[PlateSpec] | None = None,
    output_dir: Path | None = None,
    job_id: str = "job",
    job_name: str = "",
    allow_rotation: bool = True,
    layout: LayoutMode = DEFAULT_LAYOUT_MODE,
    default_plate_size: tuple[float, float] | None = None,
    default_plate_clearance: tuple[float, float] | None = None,
    job_spec: JobSpec | None = None,
    job_config: JobConfig | None = None,
    available_cutters: list[float] | None = None,
    capture: ReportCapture | None = None,
    stage_timings: StageTimings | None = None,
) -> PerCutterExport:
    """Phase 3: Export per-cutter PLT files using the clean Phase 3 pipeline.

    Renders each label independently via ``render_label_to_plt`` (which uses
    the matplotlib TTF text renderer with a custom, lossless HPGL writer),
    bin-packs labels onto plates using their rendered dimensions, then
    assembles the per-label PLT content at packed positions and splits the
    assembly by CUTTER:

    - borders + holes -> one ``[<plate>_][<material>_]<cutter>_bh_<job_id>.plt``
      per plate
    - text -> one ``[<plate>_][<material>_]<cutter>[_<color>]_txt_<job_id>.plt``
      per plate and (cutter, stroke-color) layer; colored layers gain a
      1-letter tag, colorless jobs keep the cutter-only name. The cutter
      token is the diameter in integer thousandths of an inch, unpadded
      (``0.060`` -> ``60``). PLT names are capped at 39 characters
      including the extension (the plotter controller rejects longer
      names). The plate
      number is omitted whenever the plate's material spans a single sheet
      and the sanitized material tag is omitted for material-agnostic plates
    - the combined per-plate PLT stays in memory only (returned in
      ``combined_by_plate`` for the Phase 4 color plots)

    PLT files land in ``<output_dir>/plt/`` and simple-outline PDF
    previews in ``<output_dir>/pdf/``.

    Args:
        resolved_labels: List of fully resolved labels from the resolution step.
        provided_plates: Optional list of PlateSpec objects. If None,
            unbounded mode auto-allocates default plates sized by
        job_spec: JobSpec object from parsing (for tool_options header generation).
        job_config: JobConfig object from loading job-config.json.
            ``default_plate_size`` (matching ``export_per_cutter_plts``
            behavior), overflowing onto as many plates as needed.
        output_dir: Optional output directory. Defaults to
            ``test_output/integration_test``.
        job_id: Filesystem-safe job identifier used as the file-name prefix.
        job_name: Human-readable job name for plot titles (optional).
        allow_rotation: If True (the default), the bin packer may rotate
            labels 90 degrees for tighter layouts (job-level flag).
        layout: Preferential plate fill order (job-level value; a plate may
            override it via ``PlateSpec.layout``).
        default_plate_size: ``(width, height)`` override for auto-allocated
            unbounded bins (from ``job-config.json``).
        default_plate_clearance: ``(left, top)`` edge clearance applied to
            every auto-allocated unbounded bin (from ``job-config.json``).
        available_cutters: Shop cutter inventory (``tools.json``); enables the
            compression-driven cutter reduction pre-pass when provided.
        capture: Optional :class:`ReportCapture` handler attached to the
            export logger so Phase 5's report carries the pipeline's
            WARNING/ERROR records (mirrors the ``generate`` CLI).
        stage_timings: Optional per-stage timing collector forwarded to
            ``export_per_cutter_plts``; Phase 5's report publishes the
            numbers (the stage records are INFO level, so the collector is
            what makes them survive a quieter logger).

    Returns:
        The :class:`PerCutterExport` with written PLT/PDF paths and the
        in-memory combined content per plate.
    """
    print_separator("PHASE 3: VECTORIZATION AND EXPORT (PER-CUTTER)")

    workspace = REPO_ROOT
    if output_dir is None:
        output_dir = workspace / "test_output" / "integration_test"
    elif not output_dir.is_absolute():
        output_dir = workspace / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Exporting to: {output_dir}")
    logger.info(f"DEBUG: job_spec is None: {job_spec is None}")
    logger.info(f"DEBUG: job_config is None: {job_config is None}")
    if job_spec and hasattr(job_spec, "tool_options"):
        logger.info(f"DEBUG: job_spec.tool_options: {job_spec.tool_options}")
    if job_config and hasattr(job_config, "defaults"):
        logger.info(
            f"DEBUG: job_config.defaults.tool_options exists: {hasattr(job_config.defaults, 'tool_options')}"
        )

    # A TextLogger mirrors the generate CLI's topology (console +
    # logs_generate/generate.log) so the per-layer rapid-travel reports —
    # baseline -> optimized, the routing/inter-chunk breakdown, and the
    # direction-sweep INFO line — surface in the integration run.
    # propagate=False: the script's root basicConfig handler would print
    # every record a second time.
    export_logger = TextLogger(log_file=REPO_ROOT / "logs_generate" / "generate.log")
    export_logger.logger.propagate = False
    if capture is not None:
        # The capture sits on the package logger, so every child logger in
        # the pipeline (plt_optimizer.generate.*, the optimizer's) propagates
        # its WARNING/ERROR records into it, exactly as in the CLI.
        export_logger.logger.addHandler(capture)

    export_result = export_per_cutter_plts(
        resolved_labels,
        provided_plates,
        output_dir=output_dir,
        job_id=job_id,
        job_name=job_name,
        optimize=True,
        plots=True,
        allow_rotation=allow_rotation,
        layout=layout,
        default_plate_size=default_plate_size,
        default_plate_clearance=default_plate_clearance,
        job_spec=job_spec,
        job_config=job_config,
        logger=export_logger,
        available_cutters=available_cutters,
        stage_timings=stage_timings,
    )

    print("\n--- EXPORT RESULTS ---\n")
    for path in export_result.plt_paths:
        print(f"✓ Exported: {path.relative_to(workspace)}")
    for path in export_result.pdf_paths:
        print(f"✓ Plotted:  {path.relative_to(workspace)}")

    return export_result


# ============================================================================
# PHASE 3.5: COORDINATE VALIDATION
# ============================================================================
def phase_3_5_validate_coordinates(exported_paths: list[Path]) -> None:
    """Phase 3.5: Validate coordinate ranges in exported PLT files.

    PLT files should have only positive coordinates:
    - All x-coordinates must be >= 0
    - All y-coordinates must be >= 0

    Args:
        exported_paths: List of exported PLT file paths.

    Raises:
        AssertionError: If any coordinates violate constraints.
    """
    print_separator("PHASE 3.5: COORDINATE VALIDATION")

    for plt_path in exported_paths:
        logger.info(f"Validating {plt_path.name}...")
        content = plt_path.read_text()

        # Extract all coordinates from PA/PU/PD commands
        coord_pattern = r"(PA|PU|PD)([\d,\-]+)"
        x_coords = []
        y_coords = []

        for _cmd, coords_str in re.findall(coord_pattern, content):
            coords = coords_str.split(",")
            if len(coords) >= 2:
                try:
                    for i in range(0, len(coords) - 1, 2):
                        x = int(coords[i])
                        y = int(coords[i + 1])
                        x_coords.append(x)
                        y_coords.append(y)
                except (ValueError, IndexError):
                    pass

        if not x_coords or not y_coords:
            logger.warning(f"  No coordinates found in {plt_path.name}")
            continue

        min_x, max_x = min(x_coords), max(x_coords)
        min_y, max_y = min(y_coords), max(y_coords)

        # Validate constraints
        assert min_x >= 0, f"{plt_path.name}: X has negative values (min={min_x})"
        assert min_y >= 0, f"{plt_path.name}: Y has negative values (min={min_y})"

        print(f"✓ {plt_path.name}:")
        x0_in = units_to_inches(min_x)
        x1_in = units_to_inches(max_x)
        y0_in = units_to_inches(min_y)
        y1_in = units_to_inches(max_y)
        print(f"    X range: [{min_x:8d}, {max_x:8d}] ({x0_in:.3f}, {x1_in:.3f} inches)")
        print(f"    Y range: [{min_y:8d}, {max_y:8d}] ({y0_in:.3f}, {y1_in:.3f} inches)")
        print("    Coordinates valid: all X≥0, all Y≥0 ✓")


# ============================================================================
# PHASE 3.6: LAYOUT REPORT
# ============================================================================
def phase_3_6_layout_report(
    resolved_labels: list[ResolvedLabel],
    export_result: PerCutterExport,
) -> None:
    """Phase 3.6: Report per-text-line layout effects applied at render time.

    Covers the two render-time typography effects: horizontal compression
    (margin-overflow via ``max_h_compress`` plus text-hole collision
    compression, reported as the *effective* per-line scale: ``1.000``
    natural width, ``0.870`` = 13.0% compressed) and vertical line
    spacing (the effective gap below each line after the render-time
    margin clamp, with the requested value in parentheses whenever the
    clamp changed it). Both numbers come from the render cache the export
    step exposes on ``PerCutterExport.rendered_labels``.

    The lines come from the shared formatter
    :func:`plt_optimizer.generate.layout_report.format_layout_report`
    (same source the ``generate`` CLI prints), so the two surfaces can
    never diverge.

    Args:
        resolved_labels: Resolved labels from Phase 2 (content order and
            line text for the report).
        export_result: The per-cutter export carrying the render cache.
    """
    print_separator("PHASE 3.6: LAYOUT REPORT")

    rendered_by_id = export_result.rendered_labels
    if not rendered_by_id:
        print("\u2139 No rendered labels available; layout report skipped.")
        return

    for line in format_layout_report(resolved_labels, rendered_by_id, full=True):
        print(line)
    print()


# ============================================================================
# PHASE 4: VISUALIZATION (Optional)
# ============================================================================
def phase_4_visualization(export_result: PerCutterExport, job_name: str = "") -> list[Path]:
    """Phase 4: Generate color-coded PDF previews using the plotter.

    The simple-outline previews (one per per-cutter PLT plus one combined
    ``*_all_*.pdf`` per plate) were already written by the export step into
    ``pdf/``. This phase adds the color-coded default plots (with rapid
    travel visualization):

    - one ``<plt-stem>_default.pdf`` per per-cutter PLT file.

    The combined per-plate default plot (text + borders + holes together
    in one color-coded view) is deliberately NOT generated here -- the
    per-cutter default plots plus the simple combined ``*_all_*.pdf``
    preview already cover both views.

    These color-coded plots are strictly opt-in via the module-level
    :data:`GENERATE_DEFAULT_PLOTS` flag; by default this phase is a no-op.
    The plotting itself is shared with the export pipeline via
    :func:`write_default_plots`.

    Args:
        export_result: The per-cutter export whose PLTs and combined
            content drive the plots.
        job_name: Human-readable job name for plot titles (optional).

    Returns:
        The ``*_default.pdf`` paths that were written (empty when the phase
        is disabled or the plotting failed), so Phase 5's report can list
        them alongside the export's own artifacts.
    """
    print_separator("PHASE 4: VISUALIZATION (OPTIONAL)")

    if not GENERATE_DEFAULT_PLOTS:
        print(
            "ℹ Default (color-coded) plots disabled; set GENERATE_DEFAULT_PLOTS = True to enable."
        )
        return []

    try:
        pdf_paths = write_default_plots(
            export_result.output_dir,
            export_result.job_id,
            export_result,
            job_name=job_name,
            include_combined=False,
        )
        for pdf_path in pdf_paths:
            print(f"✓ Generated: {pdf_path.name}")
        return pdf_paths
    except Exception as e:
        logger.warning(f"Visualization failed (optional): {e}")
        print(f"⚠ Visualization skipped: {e}")
        return []


# ============================================================================
# PHASE 5: GENERATION REPORT
# ============================================================================
def phase_5_generation_report(
    job: JobSpec,
    job_yaml: Path,
    export_result: PerCutterExport,
    resolved_labels: list[ResolvedLabel],
    inventory: list[float],
    boundary_hole_cutter: float | None,
    job_config: JobConfig | None,
    capture: ReportCapture,
    stage_timings: StageTimings,
    started_at: str,
    duration_seconds: float,
    default_pdf_paths: list[Path] | None = None,
) -> Path:
    """Phase 5: Write the ``generate`` pipeline's report artifacts.

    Produces the same pair the ``generate`` CLI writes, in the same place
    (``<output_dir>/generation_report/``, a sibling of ``plt/`` and
    ``pdf/``):

    - ``report.json`` -- the machine-readable document (job metadata, the
      headline counts, per-stage timings, the written outputs with the
      packed placements, and the per-label/per-line findings)
    - ``summary.txt`` -- the human-readable digest of the same data,
      echoed to the console

    The data is gathered from the same objects the CLI uses: Phase 3's
    export ran with a :class:`ReportCapture` handler on the pipeline logger
    (the WARNING/ERROR records) and a :class:`StageTimings` collector (the
    per-stage wall clock, which the INFO-level timing records would not
    survive), and the render cache on ``PerCutterExport`` supplies the
    per-line findings.

    Args:
        job: The parsed job spec (name, plate count for the summary).
        job_yaml: The job spec path recorded in the report.
        export_result: The per-cutter export (output dir, PLT/PDF paths,
            render cache, packed plates).
        resolved_labels: Resolved labels from Phase 2.
        inventory: Cutter inventory from ``tools.json`` (``job.tools``).
        boundary_hole_cutter: Requested boundary/hole cutter size.
        job_config: Loaded shop defaults (the kerf buffers in ``job.tools``).
        capture: The log capture to read the WARNING/ERROR records from.
        stage_timings: The per-stage timing collector.
        started_at: The run's start timestamp (ISO 8601).
        duration_seconds: The run's wall-clock duration.
        default_pdf_paths: The color-coded plots Phase 4 wrote.

    Returns:
        The ``summary.txt`` path that was written.
    """
    print_separator("PHASE 5: GENERATION REPORT")

    job_name, job_id, plate_count = _job_identity(job, job_yaml)
    report_dir = report_dir_for(export_result.output_dir)
    report = build_report(
        job_name=job_name,
        job_id=job_id,
        spec_path=job_yaml,
        output_dir=export_result.output_dir,
        report_dir=report_dir,
        status="success",
        started_at=started_at,
        finished_at=utc_now_iso(),
        duration_seconds=duration_seconds,
        tools=_report_tools(inventory, boundary_hole_cutter, job_config),
        options=_report_options(),
        plate_count=plate_count,
        resolved_labels=resolved_labels,
        rendered_by_id=export_result.rendered_labels,
        packed_plates=export_result.packed_plates,
        plt_files=export_result.plt_paths,
        pdf_files=export_result.pdf_paths,
        default_pdf_files=list(default_pdf_paths or []),
        records=capture.records,
        stage_records=capture.stage_records,
        timings=stage_timings,
    )
    json_path, summary_path = write_report(report_dir, report)

    print(f"✓ Report:   {_display_path(json_path)}")
    print(f"✓ Summary:  {_display_path(summary_path)}")
    print()
    print(format_summary(report))

    return summary_path


# ============================================================================
# MAIN EXECUTION
# ============================================================================
def _run_single_spec(spec_override: Path | None) -> int:
    """Execute the full end-to-end integration test pipeline for one spec.

    Args:
        spec_override: Optional path to an alternative job spec YAML (e.g.
            ``tests_deps/complex_test_job.yaml``). If None, defaults to
            ``tests_deps/test123_spec.yaml``.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    # Run bookkeeping shared by BOTH report paths. Like the generate CLI, an
    # integration run leaves ``generation_report/report.json`` +
    # ``summary.txt`` behind whether it succeeded or blew up, so the reason
    # is on disk and not only in the console scrollback. The capture and the
    # timing collector are per-run: directory mode gives every spec its own.
    started_at = utc_now_iso()
    start = time.perf_counter()
    stage = "prep"
    capture = ReportCapture()
    stage_timings = StageTimings()
    job_yaml = (
        spec_override
        if spec_override is not None
        else REPO_ROOT / "tests_deps" / "test123_spec.yaml"
    )
    if not job_yaml.is_absolute():
        job_yaml = REPO_ROOT / job_yaml
    output_dir = _resolve_output_dir(job_yaml, spec_override)
    job: JobSpec | None = None
    job_config: JobConfig | None = None
    inventory: list[float] = []
    boundary_hole_cutter: float | None = None
    resolved_labels: list[ResolvedLabel] = []
    export_result: PerCutterExport | None = None

    try:
        # Phase 1: Data Preparation
        job_yaml, tools_json, inventory, boundary_hole_cutter, job_config_json = phase_1_data_prep(
            spec_override
        )
        output_dir = _resolve_output_dir(job_yaml, spec_override)

        # Phase 2: Resolution and Layout (nominal-dimension packing for reporting)
        (
            resolved_labels,
            packed_plates,
            provided_plates,
            job_id,
            job_name,
            allow_rotation,
            layout,
            default_plate_size,
            default_plate_clearance,
            job,
            job_config,
        ) = phase_2_resolution_and_layout(
            job_yaml,
            inventory,
            boundary_hole_cutter,
            job_config_json,
            stage_timings=stage_timings,
        )

        # Phase 3: Per-cutter export using the clean bounds-aware pipeline.
        # The export renders labels onto per-cutter pens, assembles each
        # plate in memory, and writes plt/ + pdf/ outputs named
        # <plate>_{text|bh}_<cutter>_<job_id>.(plt|pdf).
        stage = "export"
        export_result = phase_3_vectorization_and_export(
            resolved_labels,
            provided_plates,
            output_dir,
            job_id,
            job_name,
            allow_rotation,
            layout,
            default_plate_size,
            default_plate_clearance,
            job,
            job_config,
            available_cutters=inventory,
            capture=capture,
            stage_timings=stage_timings,
        )

        # Phase 3.5: Coordinate Validation (on the written per-cutter PLTs)
        stage = "validate"
        phase_3_5_validate_coordinates(export_result.plt_paths)

        # Phase 3.6: Applied layout report (render-time compression +
        # line-spacing data from the export's render cache).
        phase_3_6_layout_report(resolved_labels, export_result)

        # Phase 4: Visualization (optional)
        stage = "plots"
        default_pdf_paths = phase_4_visualization(export_result, job_name=job_name)

        # Phase 5: the generation report (report.json + summary.txt) -- the
        # same artifacts the `generate` CLI writes, from the same builder.
        stage = "report"
        phase_5_generation_report(
            job=job,
            job_yaml=job_yaml,
            export_result=export_result,
            resolved_labels=resolved_labels,
            inventory=inventory,
            boundary_hole_cutter=boundary_hole_cutter,
            job_config=job_config,
            capture=capture,
            stage_timings=stage_timings,
            started_at=started_at,
            duration_seconds=time.perf_counter() - start,
            default_pdf_paths=default_pdf_paths,
        )

        print_separator("INTEGRATION TEST COMPLETE")
        print("✓ Pipeline executed successfully")
        print()
        print("Comparison:")
        print("1. Inspect the generated artifacts under the output directory:")
        print("   - plt/: per-cutter toolpath files")
        print(
            "       [<plate>_][<material>_]<cutter>[_<color>]_txt_<job>.plt: "
            "one file per text layer"
        )
        print("       [<plate>_][<material>_]<cutter>_bh_<job>.plt: borders + drill holes together")
        print(
            "       (plate number only when a material spans several sheets; "
            "material tag only when set)"
        )
        print("   - pdf/: simple-outline previews")
        print(
            "       [<plate>_][<material>_]all_<job>.pdf: combined text + borders + holes per plate"
        )
        if GENERATE_DEFAULT_PLOTS:
            print("       *_default.pdf: color-coded toolpath with rapid travel")
        print("   - generation_report/ (the generate pipeline's report surface)")
        print("       report.json: machine-readable job report (findings, timings, outputs)")
        print("       summary.txt: human-readable digest of the same data")
        print()
        print("2. Compare the simple-outline PDFs with the reference plots:")
        print("   - Reference test: test_output/test_ref_plot.png")
        print("   - Reference borders: test_output/test_ref_borders.png")
        print()
        print("   Use only the simple_mode plots for accurate comparison.")
        print()

        return 0

    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        print_separator("INTEGRATION TEST FAILED")
        print(f"✗ Error: {e}")
        _write_failure_report_for_run(
            stage=stage,
            error=e,
            job_yaml=job_yaml,
            output_dir=output_dir,
            job=job,
            job_config=job_config,
            inventory=inventory,
            boundary_hole_cutter=boundary_hole_cutter,
            resolved_labels=resolved_labels,
            export_result=export_result,
            capture=capture,
            stage_timings=stage_timings,
            started_at=started_at,
            duration_seconds=time.perf_counter() - start,
        )
        return 1
    finally:
        # Detach the capture so a directory run's next spec never inherits
        # this run's warnings (the plt_optimizer logger is reused in-process).
        capture.close()


def _write_failure_report_for_run(
    *,
    stage: str,
    error: BaseException,
    job_yaml: Path,
    output_dir: Path,
    job: JobSpec | None,
    job_config: JobConfig | None,
    inventory: list[float],
    boundary_hole_cutter: float | None,
    resolved_labels: list[ResolvedLabel],
    export_result: PerCutterExport | None,
    capture: ReportCapture,
    stage_timings: StageTimings,
    started_at: str,
    duration_seconds: float,
) -> None:
    """Write ``report.json`` + ``summary.txt`` for a run that failed.

    The failure report carries the exception type, its message and the
    pipeline phase it happened in, plus every warning logged before the
    failure and whatever the export had already written -- the same contract
    the ``generate`` CLI keeps (see
    :func:`plt_optimizer.cli.generate._write_failure_report`). Writing the
    report is best-effort: a failure here is logged, never raised, so it can
    not mask the original error.

    Args:
        stage: The phase the failure happened in.
        error: The exception that aborted the run.
        job_yaml: The job spec being processed.
        output_dir: The run's output directory.
        job: The parsed job spec (``None`` when parsing itself failed).
        job_config: Loaded shop defaults, when available.
        inventory: Cutter inventory, when loaded.
        boundary_hole_cutter: Requested boundary/hole cutter size.
        resolved_labels: Labels resolved before the failure (may be empty).
        export_result: The export, when Phase 3 had finished.
        capture: The log capture holding the warnings seen so far.
        stage_timings: The per-stage timing collector.
        started_at: The run's start timestamp (ISO 8601).
        duration_seconds: The run's wall-clock duration so far.
    """
    job_name, job_id, plate_count = _job_identity(job, job_yaml)
    report_dir = report_dir_for(output_dir)
    report = build_report(
        job_name=job_name,
        job_id=job_id,
        spec_path=job_yaml,
        output_dir=output_dir,
        report_dir=report_dir,
        status="failed",
        error={"type": type(error).__name__, "message": str(error), "stage": stage},
        started_at=started_at,
        finished_at=utc_now_iso(),
        duration_seconds=duration_seconds,
        tools=_report_tools(inventory, boundary_hole_cutter, job_config),
        options=_report_options(),
        plate_count=plate_count,
        resolved_labels=resolved_labels,
        rendered_by_id=export_result.rendered_labels if export_result is not None else None,
        packed_plates=export_result.packed_plates if export_result is not None else (),
        plt_files=export_result.plt_paths if export_result is not None else (),
        pdf_files=export_result.pdf_paths if export_result is not None else (),
        default_pdf_files=export_result.default_pdf_paths if export_result is not None else (),
        records=capture.records,
        stage_records=capture.stage_records,
        timings=stage_timings,
    )
    try:
        json_path, summary_path = write_report(report_dir, report)
    except OSError as exc:
        logger.error(f"Could not write the failure report to {report_dir}: {exc}")
        return
    print(f"✓ Failure report: {json_path}")
    print(f"✓ Failure summary: {summary_path}")
    print()


def _collect_spec_paths(spec_dir: Path) -> list[Path]:
    """Expand a job-spec directory into the YAML files to run.

    Args:
        spec_dir: Directory containing job spec YAML files (scanned
            non-recursively).

    Returns:
        Sorted list of ``*.yaml`` / ``*.yml`` files inside the directory.

    Raises:
        FileNotFoundError: If the directory contains no YAML job specs.
    """
    spec_paths = sorted(
        path
        for path in spec_dir.iterdir()
        if path.is_file() and path.suffix.lower() in (".yaml", ".yml")
    )
    if not spec_paths:
        raise FileNotFoundError(f"No YAML job specs found in directory: {spec_dir}")
    return spec_paths


def main(argv: list[str] | None = None) -> int:
    """Execute the full end-to-end integration test pipeline.

    Args:
        argv: Optional CLI arguments. An optional positional argument
            selects an alternative job spec YAML (e.g.
            ``tests_deps/complex_test_job.yaml``) or a directory of job spec
            YAML files (e.g. ``tests_deps``); a directory runs every
            ``*.yaml`` / ``*.yml`` file it contains, in sorted order, and
            prints a PASS/FAIL summary at the end. Defaults to
            ``tests_deps/test123_spec.yaml``.

    Returns:
        Exit code (0 for success, 1 if any spec failed).
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return _run_single_spec(None)

    spec_override = Path(argv[0])
    if not spec_override.is_absolute():
        spec_override = REPO_ROOT / spec_override

    if not spec_override.is_dir():
        return _run_single_spec(spec_override)

    try:
        spec_paths = _collect_spec_paths(spec_override)
    except FileNotFoundError as e:
        logger.error(f"Directory mode failed: {e}")
        print_separator("INTEGRATION TEST FAILED")
        print(f"✗ Error: {e}")
        return 1

    print_separator(f"DIRECTORY MODE: {len(spec_paths)} YAML job specs in {spec_override}")

    results: list[tuple[Path, int]] = []
    for index, spec_path in enumerate(spec_paths, start=1):
        print_separator(f"RUN {index}/{len(spec_paths)}: {spec_path.name}")
        results.append((spec_path, _run_single_spec(spec_path)))

    print_separator("DIRECTORY RUN SUMMARY")
    exit_code = 0
    for spec_path, code in results:
        status = "PASS" if code == 0 else "FAIL"
        print(f"  [{status}] {spec_path.name}")
        exit_code = max(exit_code, code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
