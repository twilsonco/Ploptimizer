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

# Import pipeline components
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.diagnostics.plotter import plot_plt_document
from plt_optimizer.generate.layout import generate_layout, generate_layout_with_bounds
from plt_optimizer.generate.resolution import (
    resolve_job_spec,
)
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.vectorize import (
    assemble_plt_from_rendered_labels,
)


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
    with open(inventory_path) as f:
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
    job_yaml = workspace / "examples" / "test123_spec.yaml"
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
    packed_plates = generate_layout(resolved_labels, job.plates)

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

    return resolved_labels, packed_plates


def _extract_layer_from_plt_file(plt_file_path: Path, target_pen: int, output_path: Path) -> None:
    """Extract a single layer from a post-processed PLT file by pen ID.

    Filters the PLT file to only include commands for the target pen,
    preserving all PA/PU/PD commands and SP selections for that pen.

    Args:
        plt_file_path: Path to the source PLT file with all layers.
        target_pen: Pen ID to extract (e.g., 1 for text, 2 for borders).
        output_path: Path to write the extracted layer.
    """
    content = plt_file_path.read_text()

    # Split by SP (Select Pen) commands to identify sections
    lines = []
    in_header = True
    current_pen = None

    # Write header and filter content by pen
    for line in content.split(";"):
        line = line.strip()
        if not line:
            continue

        # Check for SP command (Select Pen)
        if line.startswith("SP"):
            try:
                pen_id = int(line[2:])
                current_pen = pen_id
                # Only include SP commands for our target pen or pen 0 (end)
                if pen_id == target_pen or pen_id == 0:
                    lines.append(f"SP{pen_id};")
            except (ValueError, IndexError):
                lines.append(f"{line};")
        elif current_pen == target_pen or in_header:
            # Include all content for target pen or header content
            if line.startswith("IN") or line.startswith("DF") or line.startswith("PS"):
                in_header = True
                lines.append(f"{line};")
            elif line and current_pen == target_pen:
                lines.append(f"{line};")

    # Write extracted layer
    result = "".join(lines)
    if result and not result.endswith("%"):
        result += "%"

    output_path.write_text(result)


# ============================================================================
# PHASE 3: OPTIMIZATION AND VISUALIZATION
# ============================================================================
def phase_3_vectorization_and_export(
    resolved_labels: list,
    provided_plates: list | None = None,
) -> list[Path]:
    """Phase 3: Assemble and export PLT files using the clean Phase 3 pipeline.

    Renders each label independently via ``render_label_to_plt`` (which uses
    the matplotlib TTF text renderer with a custom, lossless HPGL writer),
    bin-packs labels onto plates using their rendered dimensions, then
    assembles the per-label PLT content at packed positions. This avoids
    vpype's ``write_hpgl`` page-fitting compression that otherwise crushes
    small glyph geometry into repeated coordinates (unclean text).

    Exports each layer (text, boundaries, holes) as separate PLT files to
    allow independent tool/speed selection.

    Args:
        resolved_labels: List of fully resolved labels from the resolution step.
        provided_plates: Optional list of PlateSpec objects. If None, uses a
            default 24x16 plate (matching ``generate_layout`` behavior).

    Returns:
        List of exported PLT file paths.
    """
    print_separator("PHASE 3: VECTORIZATION AND EXPORT")

    # Layer ID to pen ID mapping in PLT files
    # These correspond to the SP (Select Pen) commands in the raw PLT
    PEN_TEXT = 1
    PEN_BORDERS = 2
    PEN_HOLES = 3

    workspace = Path(__file__).parent
    output_dir = workspace / "test_output" / "integration_test"
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Exporting to: {output_dir}")

    # Phase 2 (bounds-aware): render each label and pack using rendered sizes.
    packed_plates, rendered_labels_map = generate_layout_with_bounds(
        resolved_labels, provided_plates
    )

    exported_paths: list[Path] = []
    layer_names = {
        PEN_TEXT: "text",
        PEN_BORDERS: "borders",
        PEN_HOLES: "holes",
    }

    for plate in packed_plates:
        logger.info(f"Assembling PLT for plate {plate.plate_id}...")
        # Assemble complete, lossless PLT from independently rendered labels.
        plt_content = assemble_plt_from_rendered_labels(plate, rendered_labels_map)

        # Export full document as combined file (for compatibility)
        combined_path = output_dir / f"{plate.plate_id}_raw.plt"
        logger.info(f"Exporting combined PLT: {combined_path}")
        combined_path.write_text(plt_content)
        exported_paths.append(combined_path)

        # Export each layer separately
        for pen_id, layer_name in layer_names.items():
            layer_path = output_dir / f"{plate.plate_id}_{layer_name}.plt"
            logger.info(f"Extracting {layer_name} layer (pen {pen_id}) to: {layer_path}")
            try:
                _extract_layer_from_plt_file(combined_path, pen_id, layer_path)
                # Only add to exported_paths if file has actual content (not just header)
                content_text = layer_path.read_text()
                if len(content_text) > 10:  # More than just header
                    exported_paths.append(layer_path)
                else:
                    logger.info(f"Skipping empty layer: {layer_name}")
                    layer_path.unlink()  # Delete empty file
            except Exception as e:
                logger.warning(f"Failed to extract {layer_name} layer: {e}")

    print("\n--- EXPORT RESULTS ---\n")
    for path in exported_paths:
        print(f"✓ Exported: {path.relative_to(workspace)}")

    return exported_paths


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
def phase_4_visualization(exported_paths: list[Path]) -> None:
    """Phase 4: Generate PNG previews using plotter.

    Generates two plots per PLT file:
    - Default plot (color-coded with rapid travel visualization)
    - Simple mode plot (black lines only, no rapids)

    Args:
        exported_paths: List of exported PLT file paths.
    """
    print_separator("PHASE 4: VISUALIZATION (OPTIONAL)")

    try:
        parser = PLTParser()
        for plt_path in exported_paths:
            logger.info(f"Parsing {plt_path.name}...")
            document = parser.parse_file(plt_path)

            # Generate default plot (color-coded with rapid travel)
            png_path_default = plt_path.with_stem(plt_path.stem + "_default").with_suffix(".png")
            logger.info(f"Plotting default mode to {png_path_default.name}...")
            plot_plt_document(
                document, output_path=png_path_default, show_plot=False, simple_mode=False
            )
            print(f"✓ Generated: {png_path_default.relative_to(Path.cwd())}")

            # Generate simple mode plot (black lines only, no rapids)
            png_path_simple = plt_path.with_stem(plt_path.stem + "_simple_outline").with_suffix(
                ".png"
            )
            logger.info(f"Plotting simple mode to {png_path_simple.name}...")
            plot_plt_document(
                document, output_path=png_path_simple, show_plot=False, simple_mode=True
            )
            print(f"✓ Generated: {png_path_simple.relative_to(Path.cwd())}")
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

        # Phase 2: Resolution and Layout (nominal-dimension packing for reporting)
        resolved_labels, packed_plates = phase_2_resolution_and_layout(job_yaml, inventory)

        # Phase 3: Vectorization and Export using the clean bounds-aware pipeline.
        # Uses a default 24x16 plate (same as generate_layout) so all labels pack
        # onto one sheet for comparison with the reference output.
        exported_paths = phase_3_vectorization_and_export(resolved_labels)

        # Phase 3.5: Coordinate Validation
        phase_3_5_validate_coordinates(exported_paths)

        # Phase 4: Visualization (optional)
        phase_4_visualization(exported_paths)

        print_separator("INTEGRATION TEST COMPLETE")
        print("✓ Pipeline executed successfully")
        print()
        print("Comparison:")
        print("1. Inspect the generated PNG previews in test_output/integration_test/")
        print("   - *_default.png: Color-coded toolpath with rapid travel visualization")
        print("   - *_simple_outline.png: Black lines only (for comparison with reference)")
        print()
        print("2. Compare *_simple_outline.png with the reference plots:")
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
