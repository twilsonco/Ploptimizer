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
import sys
from pathlib import Path

# Setup logging for visibility
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Import pipeline components
from plt_optimizer.generate.resolution import (
    get_cutter_diameter,
    resolve_job_spec,
)
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.layout import generate_layout
from plt_optimizer.generate.vectorize import (
    export_to_plt,
    vectorize_plate,
)
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.diagnostics.plotter import plot_plt_document


def load_tool_inventory(inventory_path: Path) -> list[float]:
    """Load available cutter diameters from tools.json.

    Args:
        inventory_path: Path to tools.json.

    Returns:
        List of available cutter diameters in inches.

    Raises:
        FileNotFoundError: If inventory_path does not exist.
        json.JSONDecodeError: If JSON is malformed.
    """
    with open(inventory_path, "r") as f:
        data = json.load(f)
    inventory = data.get("available_cutters", [])
    logger.info(f"Loaded cutter inventory: {inventory}")
    return inventory


def print_separator(title: str) -> None:
    """Print a formatted section separator."""
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80 + "\n")


# ============================================================================
# PHASE 1: TEST DATA PREPARATION
# ============================================================================
def phase_1_data_prep() -> tuple[Path, Path, list[float]]:
    """Phase 1: Load test data and inventory.

    Returns:
        Tuple of (job_yaml_path, tools_json_path, inventory).
    """
    print_separator("PHASE 1: TEST DATA PREPARATION")

    workspace = Path(__file__).parent
    job_yaml = workspace / "test_integration_job.yaml"
    tools_json = workspace / "tools.json"

    if not job_yaml.exists():
        raise FileNotFoundError(f"Test job YAML not found: {job_yaml}")
    if not tools_json.exists():
        raise FileNotFoundError(f"Tools inventory not found: {tools_json}")

    inventory = load_tool_inventory(tools_json)
    logger.info(f"Loaded job spec from: {job_yaml}")
    logger.info(f"Loaded tool inventory from: {tools_json}")

    return job_yaml, tools_json, inventory


# ============================================================================
# PHASE 2: PIPELINE EXECUTION
# ============================================================================
def phase_2_resolution_and_layout(
    job_yaml: Path,
    inventory: list[float],
) -> tuple[list, list]:
    """Phase 2: Resolution, bin packing, and verification.

    Executes:
    1. Load and parse JobSpec from YAML
    2. Resolve labels with cutter compensation
    3. Bin pack onto physical plates

    Args:
        job_yaml: Path to test job YAML.
        inventory: List of available cutter diameters.

    Returns:
        Tuple of (resolved_labels, packed_plates).
    """
    print_separator("PHASE 2: PIPELINE EXECUTION")

    # =========================================================================
    # Step 1: Parse JobSpec
    # =========================================================================
    logger.info("Step 1: Parsing JobSpec from YAML...")
    job = parse_yaml(job_yaml)
    logger.info(f"Parsed job: {job.job_name}")
    logger.info(f"Job-level text_height: {job.text_height}")
    logger.info(f"Job-level margin: {job.margin}")

    # =========================================================================
    # Step 2: Resolve labels with cutter compensation
    # =========================================================================
    logger.info("Step 2: Resolving labels with cutter compensation...")
    resolved_labels = resolve_job_spec(job, available_cutters=inventory)

    print("\n--- RESOLUTION RESULTS ---\n")
    for label in resolved_labels:
        print(f"Label ID: {label.id}")
        print(f"  Count: {label.count}")
        print(f"  Dimensions: {label.width}\" x {label.height}\"")
        print(f"  Margin: {label.margin}\"")
        print(f"  Content lines: {len(label.content)}")

        for i, line in enumerate(label.content):
            print(f"    Line {i}: '{line.text}'")
            print(f"      Nominal height: {line.nominal_text_height}\"")
            print(f"      Cutter diameter: {line.cutter_diameter}\"")
            print(f"      Toolpath height: {line.toolpath_text_height}\"")
            print(f"      Character spacing: {line.character_spacing}\"")
            print(f"      Line spacing: {line.line_spacing}\"")
        print()

    # =========================================================================
    # VERIFICATION POINT: Label C (Multi-line Kerning Test)
    # =========================================================================
    print_separator("VERIFICATION POINT 1: Cutter Selection Logic")
    label_c = next((l for l in resolved_labels if l.id == "label_c_multiline_kerning"), None)
    if label_c:
        print("Label C (Multi-line Kerning Test):")
        print(f"  Nominal text height: {label_c.content[0].nominal_text_height}\"")
        print(f"  Selected cutter: {label_c.content[0].cutter_diameter}\"")
        print(f"  Toolpath height: {label_c.content[0].toolpath_text_height}\"")
        print()

        # Manual verification of 3x tolerance logic:
        # Ideal for 0.25 is 0.03, but not in inventory [0.015, 0.02, 0.045, 0.125]
        # Closest narrower: 0.02 (distance: 0.01)
        # Closest wider: 0.045 (distance: 0.015)
        # Threshold: 0.01 > 3 * 0.015? No (0.01 < 0.045)
        # So it should prefer narrower (0.02)
        print("  EXPECTED: Cutter should be 0.02 (narrower preference)")
        print("            Because dist_narrower (0.01) <= 3 * dist_wider (0.045)")

    # =========================================================================
    # Step 3: Bin packing
    # =========================================================================
    logger.info("Step 3: Running bin packing (layout generation)...")
    packed_plates = generate_layout(resolved_labels, job.plates)

    print("\n--- BIN PACKING RESULTS ---\n")
    print(f"Total plates generated: {len(packed_plates)}")
    for plate in packed_plates:
        print(f"\nPlate {plate.plate_id}:")
        print(f"  Dimensions: {plate.width}\" x {plate.height}\"")
        print(f"  Labels packed: {len(plate.labels)}")
        for packed_label in plate.labels:
            print(f"    - {packed_label.label_id}")
            print(f"      Position: ({packed_label.x:.2f}, {packed_label.y:.2f})")
            print(f"      Size: {packed_label.width:.2f}\" x {packed_label.height:.2f}\"")
            print(f"      Rotated: {packed_label.rotated}")

    # =========================================================================
    # VERIFICATION POINT: Multi-plate allocation (Label D volume)
    # =========================================================================
    print_separator("VERIFICATION POINT 2: Multi-Plate Allocation")
    print(f"Total plates generated: {len(packed_plates)}")
    print(f"EXPECTED: At least 2 plates due to Label D (200 instances)")
    if len(packed_plates) >= 2:
        print("✓ PASS: Multi-plate allocation verified")
    else:
        print("✗ FAIL: Expected at least 2 plates")

    return resolved_labels, packed_plates


# ============================================================================
# PHASE 3: OPTIMIZATION AND VISUALIZATION
# ============================================================================
def phase_3_vectorization_and_export(
    packed_plates: list,
) -> list[Path]:
    """Phase 3: Vectorize and export to PLT files.

    Args:
        packed_plates: List of packed plates from layout phase.

    Returns:
        List of exported PLT file paths.
    """
    print_separator("PHASE 3: VECTORIZATION AND EXPORT")

    workspace = Path(__file__).parent
    output_dir = workspace / "test_output" / "integration_test"
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Exporting to: {output_dir}")

    exported_paths: list[Path] = []

    for plate in packed_plates:
        logger.info(f"Vectorizing plate {plate.plate_id}...")
        doc = vectorize_plate(plate)

        output_path = output_dir / f"{plate.plate_id}_raw.plt"
        logger.info(f"Exporting raw PLT: {output_path}")
        export_to_plt(doc, output_path, page_size=(plate.width, plate.height))
        exported_paths.append(output_path)

    print("\n--- EXPORT RESULTS ---\n")
    for path in exported_paths:
        print(f"✓ Exported: {path.relative_to(workspace)}")

    return exported_paths


# ============================================================================
# PHASE 4: VISUALIZATION (Optional)
# ============================================================================
def phase_4_visualization(exported_paths: list[Path]) -> None:
    """Phase 4: Generate PNG previews using plotter.

    Args:
        exported_paths: List of exported PLT file paths.
    """
    print_separator("PHASE 4: VISUALIZATION (OPTIONAL)")

    try:
        parser = PLTParser()
        for plt_path in exported_paths:
            png_path = plt_path.with_stem(plt_path.stem + "_preview").with_suffix(".png")
            logger.info(f"Parsing {plt_path.name}...")
            document = parser.parse_file(plt_path)
            logger.info(f"Plotting to {png_path.name}...")
            plot_plt_document(document, output_path=png_path, show_plot=False)
            print(f"✓ Generated: {png_path.relative_to(Path.cwd())}")
    except Exception as e:
        logger.warning(f"Visualization failed (optional): {e}")
        print(f"⚠ Visualization skipped: {e}")


# ============================================================================
# MAIN EXECUTION
# ============================================================================
def main() -> int:
    """Execute the full end-to-end integration test pipeline.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    try:
        # Phase 1: Data Preparation
        job_yaml, tools_json, inventory = phase_1_data_prep()

        # Phase 2: Resolution and Layout
        resolved_labels, packed_plates = phase_2_resolution_and_layout(
            job_yaml, inventory
        )

        # Phase 3: Vectorization and Export
        exported_paths = phase_3_vectorization_and_export(packed_plates)

        # Phase 4: Visualization (optional)
        phase_4_visualization(exported_paths)

        print_separator("INTEGRATION TEST COMPLETE")
        print("✓ Pipeline executed successfully")
        print()
        print("Next steps:")
        print("1. Inspect the generated PNG previews in test_output/integration_test/")
        print("2. Verify that:")
        print("   - Label C shows correct cutter selection (0.02)")
        print("   - Label D text is properly scaled with cutter compensation")
        print("   - Adjacent labels share overlapping boundary lines")
        print("   - Auto-sized Label B has correct dimensions")
        print()

        return 0

    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        print_separator("INTEGRATION TEST FAILED")
        print(f"✗ Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
