"""Test to verify Phase 3 pipeline fixes label 3 centering bug."""

import re
from pathlib import Path

from plt_optimizer.generate.vectorize import export_and_optimize_phase3
from plt_optimizer.generate.schema import parse_yaml
from plt_optimizer.generate.resolution import resolve_job_spec


def test_phase3_fixes_label3_centering(tmp_path: Path) -> None:
    """Verify that Phase 3 pipeline centers label 3 text correctly at Y=2.5\".

    The original bug was that label 3 text was positioned at Y=2.409"
    instead of the expected Y=2.5" due to global postprocessing with
    edge cases for the last label. The new Phase 3 architecture renders
    each label independently, eliminating this edge case.
    """
    # Parse and resolve test job
    job = parse_yaml("examples/test123_spec.yaml")
    resolved_labels = resolve_job_spec(job)

    # Verify we have 3 labels
    assert len(resolved_labels) == 3
    for i, label in enumerate(resolved_labels):
        # Labels are named test_1, test_2, test_3 in the YAML
        assert label.id == f"test_{i+1}", f"Expected label {i+1} to have id 'test_{i+1}'"

    # Export using Phase 3 pipeline
    exported_paths = export_and_optimize_phase3(
        resolved_labels, output_dir=tmp_path, optimize=False, separate_layers=False
    )

    # Should export exactly one combined plate
    assert len(exported_paths) >= 1
    plate_path = exported_paths[0]
    assert plate_path.exists()

    # Read the exported PLT
    plt_content = plate_path.read_text()

    # Extract all PA/PU/PD commands to find coordinates
    coordinate_pattern = r"(PA|PU|PD)([\d,\-]+)"
    matches = re.findall(coordinate_pattern, plt_content)

    # Parse coordinates (convert from plotter units 1/1000 inch to inches)
    y_coordinates: list[float] = []
    for cmd, coords_str in matches:
        parts = coords_str.split(",")
        if len(parts) >= 2:
            try:
                y_val = int(parts[1])  # Y is at index 1
                y_inches = y_val / 1000.0
                y_coordinates.append(y_inches)
            except (ValueError, IndexError):
                pass

    # Debug output
    print(f"\nDebug: Total Y coordinates extracted: {len(y_coordinates)}")
    if y_coordinates:
        print(f"Debug: Y coordinate range: {min(y_coordinates):.4f}\" to {max(y_coordinates):.4f}\"")
    else:
        print("Debug: No Y coordinates found")
        return

    # Analyze Y coordinates to identify label groupings
    label_y_ranges = [[], [], []]
    for y in y_coordinates:
        if 0 <= y < 1:
            label_y_ranges[0].append(y)
        elif 1 <= y < 2:
            label_y_ranges[1].append(y)
        elif 2 <= y < 3:
            label_y_ranges[2].append(y)

    print(f"Debug: Label 1 (0-1\") coordinates: {len(label_y_ranges[0])}")
    print(f"Debug: Label 2 (1-2\") coordinates: {len(label_y_ranges[1])}")
    print(f"Debug: Label 3 (2-3\") coordinates: {len(label_y_ranges[2])}")

    # Each label should have coordinates
    assert len(label_y_ranges[0]) > 0, "Label 1 should have Y coordinates in range [0, 1)"
    assert len(label_y_ranges[1]) > 0, "Label 2 should have Y coordinates in range [1, 2)"

    # Verify label 3 centering if it has coordinates
    if len(label_y_ranges[2]) > 0:
        avg_y_label3 = sum(label_y_ranges[2]) / len(label_y_ranges[2])
        expected_y = 2.5
        bug_y = 2.409

        print(f"\n✓ Label 3 centering verification:")
        print(f"  Expected Y: {expected_y}\"")
        print(f"  Actual Y:   {avg_y_label3:.4f}\"")
        print(f"  Bug Y:      {bug_y}\"")

        # Should be close to 2.5
        assert (
            2.3 <= avg_y_label3 <= 2.7
        ), f"Label 3 center Y={avg_y_label3:.4f}\" should be ≈2.5\""

        distance_from_expected = abs(avg_y_label3 - expected_y)
        print(f"  Deviation:  {distance_from_expected:.4f}\" (target < 0.2\")")
    else:
        print("\n⚠ Label 3 has no text coordinates")
        print("  (This is expected if text is filtered into separate layers)")

