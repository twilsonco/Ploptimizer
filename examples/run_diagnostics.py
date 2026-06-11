"""Example: Run diagnostics on a sample PLT file.

This script demonstrates how to use the PLT-Optimizer tools to:
1. Load a PLT file from disk or string content
2. Log actions using the dual logging topology (text + CSV metrics)
3. Perform identity validation by writing and re-parsing
4. Generate diagnostic plots with color-coded path visualization

Run this script to see the complete workflow in action.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

# Add project root to path for imports when running as script
sys.path.insert(0, str(Path(__file__).parent.parent))

from plt_optimizer.core.models import Coordinate, PLTDocument, StrokePath, StrokeSegment
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.writer import PLTWriter
from plt_optimizer.diagnostics.plotter import plot_plt_document
from plt_optimizer.utils.logging import (
    CSVMetricsLogger,
    TextLogger,
    get_metrics_logger,
    get_text_logger,
)


# Sample HPGL content from Cadlink EngraveLab Expert v10 for Vision 1624 table
SAMPLE_HPGL = """IN;VS0.50;ZO123,1;VZ2.00;PA;PU0.000,0.000;PD18288.000,0.000;SP;"""

# Longer sample with multiple paths
COMPLEX_SAMPLE_HPGL = """
IN;
VS0.50;
ZO123,1;
VZ2.00;
PA;
PU0.000,0.000;
PD5000.000,0.000;
PD5000.000,3000.000;
PD10000.000,3000.000;
PD10000.000,0.000;
PU15000.000,0.000;
PD20000.000,0.000;
PD20000.000,4000.000;
SP;
"""


def create_sample_plt_file(output_path: Path) -> None:
    """Create a sample PLT file for testing.

    Args:
        output_path: Destination path for the sample .plt file.
    """
    content = SAMPLE_HPGL.strip()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    print(f"Created sample PLT file: {output_path}")


def demonstrate_logging() -> None:
    """Demonstrate the dual logging topology."""
    print("\n" + "=" * 60)
    print("DEMONSTRATING DUAL LOGGING TOPOLOGY")
    print("=" * 60)

    # Get loggers
    text_logger = get_text_logger()
    metrics_logger = get_metrics_logger()

    # Text logging examples
    text_logger.info("Starting PLT optimization workflow")
    text_logger.debug(f"Processing sample HPGL content ({len(SAMPLE_HPGL)} chars)")

    # Metrics logging example
    job_id = f"demo_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    metrics_logger.log_job(
        job_id=job_id,
        original_file=Path("examples/sample.plt"),
        optimized_file=None,  # No optimization in this demo
        original_distance=18288.0,
        optimized_distance=18288.0,
        status="demo",
    )

    print(f"\nText log output: See logs/optimizer.log")
    print(f"Metrics log output: See logs/job_metrics.csv")


def demonstrate_parsing_and_writing() -> tuple[Path, Path]:
    """Demonstrate parsing, identity validation, and file writing.

    Returns:
        Tuple of (sample_path, verified_output_path).
    """
    print("\n" + "=" * 60)
    print("DEMONSTRATING PARSING AND WRITING")
    print("=" * 60)

    # Create sample file
    sample_path = Path("examples/sample.plt")
    create_sample_plt_file(sample_path)

    # Parse
    parser = PLTParser()
    text_logger = get_text_logger()

    text_logger.info(f"Parsing {sample_path}")
    doc = parser.parse_file(sample_path)

    print(f"\nParsed document structure:")
    print(f"  Header commands: {len(doc.header_commands)}")
    print(f"  Stroke paths: {len(doc.stroke_paths)}")
    print(f"  Footer commands: {len(doc.footer_commands)}")

    # Write back
    writer = PLTWriter()
    output_path = Path("examples/output_verified.plt")

    text_logger.info(f"Writing verified output to {output_path}")
    writer.write_file(doc, output_path)

    # Validate round-trip
    is_valid, errors = writer.validate_output(
        doc,
        output_path.read_text(encoding="utf-8")
    )

    print(f"\nIdentity validation: {'PASSED' if is_valid else 'FAILED'}")
    if errors:
        for error in errors:
            print(f"  Error: {error}")

    return sample_path, output_path


def demonstrate_diagnostics_plot(output_path: Path) -> Path:
    """Generate and save diagnostic plot.

    Args:
        output_path: Path to the PLT file to visualize.

    Returns:
        Path to the generated plot file.
    """
    print("\n" + "=" * 60)
    print("DEMONSTRATING DIAGNOSTIC PLOTTING")
    print("=" * 60)

    text_logger = get_text_logger()

    # Parse for plotting
    parser = PLTParser()
    doc = parser.parse_file(output_path)

    print(f"\nDocument statistics:")
    print(f"  Total segments: {doc.total_segments}")
    print(f"  Cutting distance: {doc.cutting_distance():,.2f}")
    print(f"  Rapid travel: {doc.rapid_distance():,.2f}")

    # Generate plot
    text_logger.info("Generating diagnostic plot")
    plot_path = Path("examples/toolpath_diagnostic.png")

    fig = plot_plt_document(
        doc,
        output_path=plot_path,
        title="PLT-Optimizer Diagnostic View",
    )

    print(f"\nDiagnostic plot saved to: {plot_path}")

    # Close figure to free memory
    import matplotlib.pyplot as plt
    plt.close(fig)

    return plot_path


def demonstrate_complex_sample() -> tuple[Path, Path]:
    """Demonstrate with the more complex sample.

    Returns:
        Tuple of (plt_output_path, plot_output_path).
    """
    print("\n" + "=" * 60)
    print("DEMONSTRATING COMPLEX SAMPLE")
    print("=" * 60)

    parser = PLTParser()
    writer = PLTWriter()

    text_logger = get_text_logger()
    text_logger.info("Parsing complex HPGL sample")

    doc = parser.parse_string(COMPLEX_SAMPLE_HPGL.strip())

    print(f"\nComplex document statistics:")
    print(f"  Header commands: {len(doc.header_commands)}")
    print(f"  Stroke paths: {len(doc.stroke_paths)}")
    print(f"  Total segments: {doc.total_segments}")
    print(f"  Cutting distance: {doc.cutting_distance():,.2f}")

    # Write and plot
    complex_output = Path("examples/complex_sample.plt")
    complex_plot = Path("examples/complex_toolpath.png")
    writer.write_file(doc, complex_output)

    fig = plot_plt_document(
        doc,
        output_path=complex_plot,
        title="Complex Toolpath Diagnostic",
    )

    print(f"\nComplex sample written to: {complex_output}")
    print(f"Plot saved to: {complex_plot}")

    import matplotlib.pyplot as plt
    plt.close(fig)

    return complex_output, complex_plot


def main() -> int:
    """Main entry point for the diagnostics demonstration.

    Returns:
        Exit code (0 for success).
    """
    print("PLT-Optimizer Diagnostics Demonstration")
    print("=" * 60)

    try:
        # Step 1: Demonstrate logging
        demonstrate_logging()

        # Step 2: Parse, write, and validate
        sample_path, verified_path = demonstrate_parsing_and_writing()

        # Step 3: Generate diagnostic plot for simple sample
        simple_plot_path = demonstrate_diagnostics_plot(verified_path)

        # Step 4: Demo with complex multi-path sample
        complex_plt_path, complex_plot_path = demonstrate_complex_sample()

        print("\n" + "=" * 60)
        print("DEMONSTRATION COMPLETE")
        print("=" * 60)
        print(f"\nGenerated files:")
        for path in [sample_path, verified_path, simple_plot_path, complex_plt_path, complex_plot_path]:
            exists = "\u2713" if path.exists() else "\u2717"
            print(f"  {exists} {path}")
        print(f"\nLog files:")
        log_dir = Path("logs")
        for log_file in sorted(log_dir.glob("*")):
            size = log_file.stat().st_size
            size_str = f"({size:,} bytes)" if size < 1024 else f"({size / 1024:.1f} KB)"
            print(f"  - {log_file} {size_str}")

        return 0

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())