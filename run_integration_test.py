#!/usr/bin/env python3
"""End-to-End Integration Test Runner.

This script executes the full pipeline from YAML ingestion to PLT export,
with intermediate state dumps for verification. Validates:

- Hierarchical resolution and inheritance cascade
- Cutter compensation and 3x tolerance logic
- Bin packing and multi-plate allocation
- Vectorization and PLT export

Run with: python run_integration_test.py
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

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
GENERATE_DEFAULT_PLOTS = False

# Import pipeline components
from plt_optimizer.generate.layout import generate_layout
from plt_optimizer.generate.resolution import (
    resolve_job_spec,
)
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.substitution import expand_job_spec
from plt_optimizer.generate.vectorize import (
    PerCutterExport,
    export_per_cutter_plts,
    write_default_plots,
)


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


# ============================================================================
# PHASE 1: TEST DATA PREPARATION
# ============================================================================
def phase_1_data_prep(
    job_yaml_override: Path | None = None,
) -> tuple[Path, Path, list[float], float | None]:
    """Phase 1: Load test data and inventory.

    Args:
        job_yaml_override: Optional path to an alternative job spec YAML.
            If None, defaults to ``examples/test123_spec.yaml``.

    Returns:
        Tuple of (job_yaml_path, tools_json_path, inventory,
        boundary_hole_cutter_size).
    """
    print_separator("PHASE 1: TEST DATA PREPARATION")

    workspace = Path(__file__).parent
    job_yaml = job_yaml_override or workspace / "examples" / "test123_spec.yaml"
    if not job_yaml.is_absolute():
        job_yaml = workspace / job_yaml
    tools_json = workspace / "tools.json"

    if not job_yaml.exists():
        raise FileNotFoundError(f"Test job YAML not found: {job_yaml}")
    if not tools_json.exists():
        raise FileNotFoundError(f"Tools inventory not found: {tools_json}")

    inventory, boundary_hole_cutter = load_tool_inventory(tools_json)
    logger.info(f"Loaded job spec from: {job_yaml}")
    logger.info(f"Loaded tool inventory from: {tools_json}")

    return job_yaml, tools_json, inventory, boundary_hole_cutter


# ============================================================================
# PHASE 2: PIPELINE EXECUTION
# ============================================================================
def phase_2_resolution_and_layout(
    job_yaml: Path,
    inventory: list[float],
    boundary_hole_cutter_size: float | None = None,
) -> tuple[list, list, list | None, str, bool]:
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

    Returns:
        Tuple of (resolved_labels, packed_plates, provided_plates, job_id,
        allow_rotation).
    """
    print_separator("PHASE 2: PIPELINE EXECUTION")

    # =========================================================================
    # Step 1: Parse JobSpec
    # =========================================================================
    logger.info("Step 1: Parsing JobSpec from YAML...")
    job = expand_job_spec(parse_yaml(job_yaml), job_yaml)
    logger.info(f"Parsed job: {job.job_name}")
    logger.info(f"Job-level text_height: {job.text_height}")
    logger.info(f"Job-level margin: {job.margin}")

    # =========================================================================
    # Step 2: Resolve labels with cutter compensation
    # =========================================================================
    logger.info("Step 2: Resolving labels with cutter compensation...")
    resolved_labels = resolve_job_spec(
        job,
        available_cutters=inventory,
        boundary_hole_cutter_size=boundary_hole_cutter_size,
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
            print(f'      Cutter diameter: {line.cutter_diameter}"')
            print(f'      Toolpath height: {line.toolpath_text_height}"')
        print()

    # =========================================================================
    # Step 3: Bin packing
    # =========================================================================
    logger.info("Step 3: Running bin packing (layout generation)...")
    packed_plates = generate_layout(
        resolved_labels, job.plates, allow_rotation=job.allow_rotation
    )

    print("\n--- BIN PACKING RESULTS ---\n")
    print(f"Total plates generated: {len(packed_plates)}")
    for plate in packed_plates:
        print(f"\nPlate {plate.plate_id}:")
        print(f'  Dimensions: {plate.width}" x {plate.height}"')
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

    return resolved_labels, packed_plates, job.plates, job_id, job.allow_rotation


# ============================================================================
# PHASE 3: OPTIMIZATION AND VISUALIZATION
# ============================================================================
def phase_3_vectorization_and_export(
    resolved_labels: list,
    provided_plates: list | None = None,
    output_dir: Path | None = None,
    job_id: str = "job",
    allow_rotation: bool = True,
) -> PerCutterExport:
    """Phase 3: Export per-cutter PLT files using the clean Phase 3 pipeline.

    Renders each label independently via ``render_label_to_plt`` (which uses
    the matplotlib TTF text renderer with a custom, lossless HPGL writer),
    bin-packs labels onto plates using their rendered dimensions, then
    assembles the per-label PLT content at packed positions and splits the
    assembly by CUTTER:

    - borders + holes -> one ``<plate>_bh_<cutter>_<job_id>.plt`` per plate
    - text -> one ``<plate>_text_<cutter>_<job_id>.plt`` per plate and
      cutter diameter
    - the combined per-plate PLT stays in memory only (returned in
      ``combined_by_plate`` for the Phase 4 color plots)

    PLT files land in ``<output_dir>/plt/`` and simple-outline PDF
    previews in ``<output_dir>/pdf/``.

    Args:
        resolved_labels: List of fully resolved labels from the resolution step.
        provided_plates: Optional list of PlateSpec objects. If None, uses a
            default A3 plate (matching ``export_per_cutter_plts`` behavior).
        output_dir: Optional output directory. Defaults to
            ``test_output/integration_test``.
        job_id: Filesystem-safe job identifier used as the file-name prefix.
        allow_rotation: If True (the default), the bin packer may rotate
            labels 90 degrees for tighter layouts (job-level flag).

    Returns:
        The :class:`PerCutterExport` with written PLT/PDF paths and the
        in-memory combined content per plate.
    """
    print_separator("PHASE 3: VECTORIZATION AND EXPORT (PER-CUTTER)")

    workspace = Path(__file__).parent
    if output_dir is None:
        output_dir = workspace / "test_output" / "integration_test"
    elif not output_dir.is_absolute():
        output_dir = workspace / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Exporting to: {output_dir}")

    export_result = export_per_cutter_plts(
        resolved_labels,
        provided_plates,
        output_dir=output_dir,
        job_id=job_id,
        optimize=True,
        plots=True,
        allow_rotation=allow_rotation,
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

        for cmd, coords_str in re.findall(coord_pattern, content):
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
        print(
            f"    X range: [{min_x:8d}, {max_x:8d}] ({min_x / 1000:.3f}, {max_x / 1000:.3f} inches)"
        )
        print(
            f"    Y range: [{min_y:8d}, {max_y:8d}] ({min_y / 1000:.3f}, {max_y / 1000:.3f} inches)"
        )
        print("    Coordinates valid: all X≥0, all Y≥0 ✓")


# ============================================================================
# PHASE 4: VISUALIZATION (Optional)
# ============================================================================
def phase_4_visualization(export_result: PerCutterExport) -> None:
    """Phase 4: Generate color-coded PDF previews using the plotter.

    The simple-outline previews (one per per-cutter PLT plus one combined
    ``*_all_*.pdf`` per plate) were already written by the export step into
    ``pdf/``. This phase adds the color-coded default plots (with rapid
    travel visualization):

    - one ``<plt-stem>_default.pdf`` per per-cutter PLT file, and
    - one ``<plate>_all_<job_id>_default.pdf`` per plate from the
      in-memory combined content (text + borders + holes together).

    These color-coded plots are strictly opt-in via the module-level
    :data:`GENERATE_DEFAULT_PLOTS` flag; by default this phase is a no-op.
    The plotting itself is shared with the export pipeline via
    :func:`write_default_plots`.

    Args:
        export_result: The per-cutter export whose PLTs and combined
            content drive the plots.
    """
    print_separator("PHASE 4: VISUALIZATION (OPTIONAL)")

    if not GENERATE_DEFAULT_PLOTS:
        print(
            "ℹ Default (color-coded) plots disabled; set GENERATE_DEFAULT_PLOTS = True to enable."
        )
        return

    try:
        pdf_paths = write_default_plots(
            export_result.output_dir, export_result.job_id, export_result
        )
        for pdf_path in pdf_paths:
            print(f"✓ Generated: {pdf_path.name}")
    except Exception as e:
        logger.warning(f"Visualization failed (optional): {e}")
        print(f"⚠ Visualization skipped: {e}")


# ============================================================================
# MAIN EXECUTION
# ============================================================================
def main(argv: list[str] | None = None) -> int:
    """Execute the full end-to-end integration test pipeline.

    Args:
        argv: Optional CLI arguments. An optional positional argument
            selects an alternative job spec YAML (e.g.
            ``examples/complex_test_job.yaml``). Defaults to
            ``examples/test123_spec.yaml``.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    spec_override = Path(argv[0]) if argv else None

    try:
        # Phase 1: Data Preparation
        job_yaml, tools_json, inventory, boundary_hole_cutter = phase_1_data_prep(spec_override)

        # Phase 2: Resolution and Layout (nominal-dimension packing for reporting)
        (
            resolved_labels,
            packed_plates,
            provided_plates,
            job_id,
            allow_rotation,
        ) = phase_2_resolution_and_layout(job_yaml, inventory, boundary_hole_cutter)

        # Phase 3: Per-cutter export using the clean bounds-aware pipeline.
        # The export renders labels onto per-cutter pens, assembles each
        # plate in memory, and writes plt/ + pdf/ outputs named
        # <plate>_{text|bh}_<cutter>_<job_id>.(plt|pdf).
        if spec_override is not None:
            phase_3_output_dir = Path("test_output") / "integration_test" / job_yaml.stem
        else:
            phase_3_output_dir = None
        export_result = phase_3_vectorization_and_export(
            resolved_labels, provided_plates, phase_3_output_dir, job_id, allow_rotation
        )

        # Phase 3.5: Coordinate Validation (on the written per-cutter PLTs)
        phase_3_5_validate_coordinates(export_result.plt_paths)

        # Phase 4: Visualization (optional)
        phase_4_visualization(export_result)

        print_separator("INTEGRATION TEST COMPLETE")
        print("✓ Pipeline executed successfully")
        print()
        print("Comparison:")
        print("1. Inspect the generated artifacts under the output directory:")
        print("   - plt/: per-cutter toolpath files")
        print("       <plate>_text_<cutter>_<job>.plt: one file per text cutter diameter")
        print("       <plate>_bh_<cutter>_<job>.plt: borders + drill holes together")
        print("   - pdf/: simple-outline previews")
        print("       <plate>_all_<job>.pdf: combined text + borders + holes per plate")
        if GENERATE_DEFAULT_PLOTS:
            print("       *_default.pdf: color-coded toolpath with rapid travel")
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
        return 1


if __name__ == "__main__":
    sys.exit(main())
