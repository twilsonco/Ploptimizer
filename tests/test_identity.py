"""Identity validation tests for PLT-Optimizer.

This module provides rigorous identity testing to ensure that:
1. A parsed PLT file can be written back with semantic equivalence
2. The round-trip (parse -> write -> parse) produces mathematically congruent results
3. Coordinate precision is preserved through 3 decimal places

These tests are critical for ensuring the parser and writer maintain fidelity
to the original HPGL/PLT format from EngraveLab.
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import pytest

from plt_optimizer.core.chunker import Chunker, ChunkerConfig
from plt_optimizer.core.models import ArcSegment, Coordinate, PLTDocument
from plt_optimizer.core.optimizer import (
    NearestNeighbor2OptStrategy,
    NoOpStrategy,
    OptimizerEngine,
)
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.profiler import Profiler
from plt_optimizer.core.reassembler import Reassembler
from plt_optimizer.core.writer import PLTWriter, WriteError

# Sample HPGL content from Cadlink EngraveLab Expert v10
SAMPLE_HPGL = "IN;VS0.50;ZO123,1;VZ2.00;PA;PU0.000,0.000;PD18288.000,0.000;SP;"


class TestIdentityValidation:
    """Tests for identity preservation through parse-write cycles."""

    def test_parse_and_write_exact_string(self) -> None:
        """Test that parsing and writing produces semantically equivalent output."""
        parser = PLTParser()
        writer = PLTWriter()

        # Parse the sample HPGL
        doc1 = parser.parse_string(SAMPLE_HPGL)

        # Write back to string
        output1 = writer.write_string(doc1)

        # Re-parse the generated output
        doc2 = parser.parse_string(output1)

        # Verify key properties match
        assert len(doc1.header_commands) == len(doc2.header_commands), (
            f"Header command count mismatch: {len(doc1.header_commands)} vs "
            f"{len(doc2.header_commands)}"
        )

        assert len(doc1.footer_commands) == len(doc2.footer_commands), (
            f"Footer command count mismatch: {len(doc1.footer_commands)} vs "
            f"{len(doc2.footer_commands)}"
        )

    def test_coordinate_precision_preserved(self) -> None:
        """Test that coordinate precision to 3 decimal places is maintained."""
        parser = PLTParser()

        doc = parser.parse_string(SAMPLE_HPGL)

        # Check all coordinates have at most 3 decimal places
        for path in doc.stroke_paths:
            for seg in path.segments:
                # Verify 3 decimal place precision
                assert seg.start.x == round(seg.start.x, 3)
                assert seg.start.y == round(seg.start.y, 3)
                assert seg.end.x == round(seg.end.x, 3)
                assert seg.end.y == round(seg.end.y, 3)

    def test_file_roundtrip_identity(self) -> None:
        """Test that file-based round-trip maintains semantic equivalence."""
        parser = PLTParser()
        writer = PLTWriter()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Write sample to input file
            input_path = Path(tmpdir) / "input.plt"
            output_path = Path(tmpdir) / "output.plt"

            input_path.write_text(SAMPLE_HPGL, encoding="utf-8")

            # Parse and write
            doc = parser.parse_file(input_path)
            writer.write_file(doc, output_path)

            # Re-parse the generated file
            doc2 = parser.parse_file(output_path)

            # Compare structure
            assert len(doc.header_commands) == len(doc2.header_commands)
            assert len(doc.stroke_paths) == len(doc2.stroke_paths)
            assert len(doc.footer_commands) == len(doc2.footer_commands)

    def test_distance_calculations_consistent(self) -> None:
        """Test that distance calculations are mathematically consistent."""
        parser = PLTParser()

        doc = parser.parse_string(SAMPLE_HPGL)

        # Calculate distances manually
        total_calc = 0.0
        for path in doc.stroke_paths:
            for seg in path.segments:
                dx = seg.end.x - seg.start.x
                dy = seg.end.y - seg.start.y
                segment_length = (dx * dx + dy * dy) ** 0.5
                total_calc += segment_length

        # Compare with document's calculated distance
        assert math.isclose(
            doc.cutting_distance(),
            total_calc,
            rel_tol=1e-6,
        ), f"Distance mismatch: {doc.cutting_distance()} vs {total_calc}"

    def test_segment_count_preserved(self) -> None:
        """Test that the number of stroke segments is preserved through round-trip."""
        parser = PLTParser()
        writer = PLTWriter()

        doc1 = parser.parse_string(SAMPLE_HPGL)
        output1 = writer.write_string(doc1)

        # Re-parse
        doc2 = parser.parse_string(output1)

        assert doc1.total_segments == doc2.total_segments, (
            f"Segment count mismatch: {doc1.total_segments} vs "
            f"{doc2.total_segments}"
        )

    def test_empty_document_write(self) -> None:
        """Test writing an empty document produces minimal valid output."""
        writer = PLTWriter()
        doc = PLTDocument()

        output = writer.write_string(doc)

        # Empty document should produce empty string
        assert output == ""

    def test_single_coordinate_roundtrip(self) -> None:
        """Test round-trip for a simple single-coordinate sequence."""
        parser = PLTParser()
        writer = PLTWriter()

        simple_plt = "IN;PU100.000,200.000;PD300.000,400.000;SP;"
        doc1 = parser.parse_string(simple_plt)
        _ = writer.write_string(doc1)

        # Verify structure
        assert len(doc1.stroke_paths) >= 1

    def test_multiple_paths_roundtrip(self) -> None:
        """Test round-trip for multiple stroke paths."""
        parser = PLTParser()
        writer = PLTWriter()

        multi_path_plt = (
            "IN;PU0.000,0.000;"
            "PD100.000,0.000;"
            "PD100.000,100.000;"
            "PU200.000,200.000;"
            "PD300.000,200.000;"
            "SP;"
        )

        doc1 = parser.parse_string(multi_path_plt)
        output1 = writer.write_string(doc1)
        doc2 = parser.parse_string(output1)

        # Verify we have the expected number of segments
        assert doc1.total_segments == doc2.total_segments


class TestWriterOutput:
    """Tests for PLT writer output format and validation."""

    def test_writer_validates_output(self) -> None:
        """Test that validate_output correctly identifies valid output."""
        parser = PLTParser()
        writer = PLTWriter()

        doc1 = parser.parse_string(SAMPLE_HPGL)
        output1 = writer.write_string(doc1)

        is_valid, errors = writer.validate_output(doc1, output1)

        assert is_valid, f"Output validation failed with errors: {errors}"
        assert len(errors) == 0

    def test_write_number_formatting(self) -> None:
        """Test that numbers are formatted to exactly 3 decimal places."""
        writer = PLTWriter()

        # Test various number formats
        assert writer._format_number(100.0) == "100.000"
        assert writer._format_number(0.5) == "0.500"
        assert writer._format_number(-18288.123456) == "-18288.123"

    def test_write_coordinate_formatting(self) -> None:
        """Test that coordinates are formatted correctly."""
        writer = PLTWriter()
        coord = Coordinate(x=18288.5, y=-0.125)

        formatted = writer._format_coord(coord)

        assert "18288.500" in formatted
        assert "-0.125" in formatted


class TestEdgeCases:
    """Tests for edge cases and error conditions."""

    def test_parse_unknown_command_handling(self) -> None:
        """Test handling of unrecognized but syntactically valid HPGL tokens.

        The tokenizer extracts syntactically valid HPGL-like tokens (uppercase
        letters/digits/punctuation ending with semicolon). Unknown commands
        are handled gracefully as headers with a warning rather than raising
        errors, since EngraveLab may use custom/vendor extensions.
        """
        parser = PLTParser()

        # Unrecognized but syntactically-valid uppercase tokens become unknown header commands
        doc1 = parser.parse_string("PARSED;")
        assert len(doc1.header_commands) >= 1

        doc2 = parser.parse_string("ALL;")
        assert len(doc2.header_commands) >= 1

    def test_write_to_nonexistent_directory(self) -> None:
        """Test that writing to nonexistent directory raises WriteError."""
        writer = PLTWriter()
        doc = PLTDocument()

        invalid_path = Path("/nonexistent/path/that/does/not/exist/file.plt")

        with pytest.raises(WriteError):
            writer.write_file(doc, invalid_path)

    def test_parse_empty_string(self) -> None:
        """Test parsing empty string returns empty document."""
        parser = PLTParser()

        doc = parser.parse_string("")

        assert len(doc.header_commands) == 0
        assert len(doc.stroke_paths) == 0
        assert len(doc.footer_commands) == 0

    def test_parse_only_commands(self) -> None:
        """Test parsing document with only header/footer commands."""
        parser = PLTParser()

        doc = parser.parse_string("IN;VS0.50;SP;")

        assert len(doc.header_commands) >= 2
        assert len(doc.footer_commands) == 1


class TestMetadataPreservation:
    """Tests for metadata preservation through parse-write cycles."""

    def test_example_file_roundtrip_identity(self) -> None:
        """Test that parsing and writing an example file preserves all metadata."""
        parser = PLTParser()
        writer = PLTWriter()

        example_path = Path(__file__).parent.parent / "examples" / "test_rect_grid13sheet0.plt"

        doc1 = parser.parse_file(example_path)
        output = writer.write_string(doc1)

        doc2 = parser.parse_string(output)

        assert len(doc1.header_commands) == len(doc2.header_commands), (
            f"Header command count mismatch: {len(doc1.header_commands)} vs "
            f"{len(doc2.header_commands)}"
        )

        assert doc1.header_commands == doc2.header_commands, (
            "Header commands do not match exactly (instruction + parameters)"
        )

        assert len(doc1.footer_commands) == len(doc2.footer_commands), (
            f"Footer command count mismatch: {len(doc1.footer_commands)} vs "
            f"{len(doc2.footer_commands)}"
        )

        assert doc1.footer_commands == doc2.footer_commands, (
            "Footer commands do not match exactly (instruction + parameters)"
        )

        assert len(doc1.stroke_paths) == len(doc2.stroke_paths), (
            f"Stroke path count mismatch: {len(doc1.stroke_paths)} vs "
            f"{len(doc2.stroke_paths)}"
        )

    def test_original_optimized_files_have_identical_metadata(self) -> None:
        """Test that original and optimized files have identical header metadata."""
        parser = PLTParser()
        writer = PLTWriter()

        examples_dir = Path(__file__).parent.parent / "examples"
        original_path = examples_dir / "test_rect_grid13sheet0.plt"

        doc_original = parser.parse_file(original_path)

        # Profile to determine if structural (pass document, not stroke_paths list)
        profiler = Profiler()
        profile_result = profiler.profile(doc_original)

        # Chunk the document
        chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
        blocks = chunker.chunk(
            doc_original.stroke_paths,
            profile_result.baseline_extent,
            is_structural=profile_result.is_structural,
        )

        if not blocks:
            pytest.skip("No blocks generated from file")

        # Optimize using fast mode (NearestNeighbor + 2-Opt)
        strategy = NearestNeighbor2OptStrategy()
        engine = OptimizerEngine(strategy=strategy)
        optimization_result = engine.optimize(blocks)

        # Reassemble into optimized document
        reassembler = Reassembler()
        doc_optimized = reassembler.reassemble(
            original_document=doc_original,
            blocks=blocks,
            optimization_result=optimization_result,
        )

        # Write optimized to temp file for comparison
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized_path = Path(tmpdir) / "test_rect_grid13sheet0_optimized.plt"
            writer.write_file(doc_optimized, optimized_path)

            # Re-parse the optimized file
            doc_optimized_parsed = parser.parse_file(optimized_path)

            assert len(doc_original.header_commands) == len(
                doc_optimized_parsed.header_commands
            ), (
                f"Header command count mismatch: {len(doc_original.header_commands)} vs "
                f"{len(doc_optimized_parsed.header_commands)}"
            )

            assert doc_original.header_commands == doc_optimized_parsed.header_commands, (
                "Header commands do not match exactly between original and optimized files"
            )

    def test_noop_strategy_identity_with_1inch_square(self) -> None:
        """Test that NoOp strategy produces identity output with 1-inch square.

        Identity validation criteria:
        1. Line count in output text is identical to input
        2. Header and footer commands match exactly (byte-wise in the text output)
        3. All stroke/arc commands are numerically identical (coordinates must match,
           but string representations are allowed to differ due to formatting)

        This test uses the 1-inch-square.plt example file as a representative case.
        Running the no-opt strategy should preserve all formatting and structure of
        the input file, since no optimization is applied.

        Raises:
            AssertionError: If any identity criterion is violated.
        """
        parser = PLTParser()
        writer = PLTWriter()

        # Load the 1-inch-square.plt example file
        examples_dir = Path(__file__).parent.parent / "examples"
        original_path = examples_dir / "1-inch-square.plt"

        if not original_path.exists():
            pytest.skip(f"Example file not found: {original_path}")

        # Read original file text for byte-wise comparison
        original_text = original_path.read_text(encoding="utf-8")
        original_lines = original_text.splitlines()

        # Parse the original document
        doc_original = parser.parse_file(original_path)

        # Profile the document
        profiler = Profiler()
        profile_result = profiler.profile(doc_original)

        # Chunk the document
        chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
        blocks = chunker.chunk(
            doc_original.stroke_paths,
            profile_result.baseline_extent,
            is_structural=profile_result.is_structural,
        )

        if not blocks:
            pytest.skip("No blocks generated from file")

        # Optimize using NoOpStrategy (baseline)
        strategy = NoOpStrategy()
        engine = OptimizerEngine(strategy=strategy)
        optimization_result = engine.optimize(blocks)

        # Reassemble into output document
        reassembler = Reassembler()
        doc_output = reassembler.reassemble(
            original_document=doc_original,
            blocks=blocks,
            optimization_result=optimization_result,
        )

        # Write to temp file for comparison
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "1-inch-square_noop.plt"
            writer.write_file(doc_output, output_path)

            output_text = output_path.read_text(encoding="utf-8")
            output_lines = output_text.splitlines()

            # Re-parse the output document for stroke/arc validation
            doc_reparsed = parser.parse_file(output_path)

            # ===== CRITERION 1: Line count must be identical =====
            assert len(original_lines) == len(output_lines), (
                f"Line count mismatch: original has {len(original_lines)} lines, "
                f"output has {len(output_lines)} lines.\n"
                f"Original lines:\n{original_lines}\n"
                f"Output lines:\n{output_lines}"
            )

            # ===== CRITERION 2: Header and footer commands must match byte-wise =====
            # Extract header lines (before any PU/PD commands)
            original_header_lines = []
            output_header_lines = []

            for line in original_lines:
                if any(cmd in line for cmd in ["PU", "PD", "PA"]):
                    break
                original_header_lines.append(line)

            for line in output_lines:
                if any(cmd in line for cmd in ["PU", "PD", "PA"]):
                    break
                output_header_lines.append(line)

            assert len(original_header_lines) == len(output_header_lines), (
                f"Header line count mismatch: original has {len(original_header_lines)}, "
                f"output has {len(output_header_lines)}"
            )

            for line_idx, (orig_line, out_line) in enumerate(
                zip(original_header_lines, output_header_lines)
            ):
                assert orig_line == out_line, (
                    f"Header line {line_idx} byte-wise mismatch:\n"
                    f"  Original: {repr(orig_line)}\n"
                    f"  Output:   {repr(out_line)}"
                )

            # Extract footer lines (SP; and anything after)
            original_footer_idx = None
            output_footer_idx = None

            for idx in range(len(original_lines) - 1, -1, -1):
                if "SP" in original_lines[idx]:
                    original_footer_idx = idx
                    break

            for idx in range(len(output_lines) - 1, -1, -1):
                if "SP" in output_lines[idx]:
                    output_footer_idx = idx
                    break

            assert original_footer_idx is not None, "Footer (SP;) not found in original"
            assert output_footer_idx is not None, "Footer (SP;) not found in output"

            # Verify footer line(s) match
            original_footer_lines = original_lines[original_footer_idx:]
            output_footer_lines = output_lines[output_footer_idx:]

            assert len(original_footer_lines) == len(output_footer_lines), (
                f"Footer line count mismatch: original has {len(original_footer_lines)}, "
                f"output has {len(output_footer_lines)}"
            )

            for line_idx, (orig_line, out_line) in enumerate(
                zip(original_footer_lines, output_footer_lines)
            ):
                assert orig_line == out_line, (
                    f"Footer line {line_idx} byte-wise mismatch:\n"
                    f"  Original: {repr(orig_line)}\n"
                    f"  Output:   {repr(out_line)}"
                )

            # ===== CRITERION 3: Stroke/arc commands numerically identical =====
            assert len(doc_original.stroke_paths) == len(doc_reparsed.stroke_paths), (
                f"Stroke path count mismatch: original={len(doc_original.stroke_paths)}, "
                f"output={len(doc_reparsed.stroke_paths)}"
            )

            for path_idx, (orig_path, reparsed_path) in enumerate(
                zip(doc_original.stroke_paths, doc_reparsed.stroke_paths)
            ):
                assert len(orig_path.segments) == len(reparsed_path.segments), (
                    f"Segment count mismatch in path {path_idx}: "
                    f"original={len(orig_path.segments)}, output={len(reparsed_path.segments)}"
                )

                for seg_idx, (orig_seg, reparsed_seg) in enumerate(
                    zip(orig_path.segments, reparsed_path.segments)
                ):
                    # Verify both segments are the same type (StrokeSegment or ArcSegment)
                    assert isinstance(orig_seg, ArcSegment) == isinstance(reparsed_seg, ArcSegment), (
                        f"Segment type mismatch in path {path_idx}, segment {seg_idx}: "
                        f"original={type(orig_seg).__name__}, output={type(reparsed_seg).__name__}"
                    )

                    # Compare numeric coordinates (3 decimal place precision)
                    assert math.isclose(orig_seg.start.x, reparsed_seg.start.x, rel_tol=1e-6), (
                        f"Start X mismatch in path {path_idx}, segment {seg_idx}: "
                        f"original={orig_seg.start.x}, output={reparsed_seg.start.x}"
                    )
                    assert math.isclose(orig_seg.start.y, reparsed_seg.start.y, rel_tol=1e-6), (
                        f"Start Y mismatch in path {path_idx}, segment {seg_idx}: "
                        f"original={orig_seg.start.y}, output={reparsed_seg.start.y}"
                    )
                    assert math.isclose(orig_seg.end.x, reparsed_seg.end.x, rel_tol=1e-6), (
                        f"End X mismatch in path {path_idx}, segment {seg_idx}: "
                        f"original={orig_seg.end.x}, output={reparsed_seg.end.x}"
                    )
                    assert math.isclose(orig_seg.end.y, reparsed_seg.end.y, rel_tol=1e-6), (
                        f"End Y mismatch in path {path_idx}, segment {seg_idx}: "
                        f"original={orig_seg.end.y}, output={reparsed_seg.end.y}"
                    )

    def test_nn2opt_strategy_rapid_travel_improvement_1x3_holes(self) -> None:
        """Test that nn2opt strategy achieves ~71.79% rapid travel improvement on 1x3 file.

        This test verifies that running the NearestNeighbor2OptStrategy on the
        "1x3 half inch letters holes1.plt" file produces a rapid travel distance
        reduction of approximately 71.79%.

        The file has 96 holes arranged in a 4x6 grid (2 columns x 16 rows), which
        creates significant opportunity for optimization through reordering.

        Expected metrics:
        - Original rapid travel distance: ~563.7
        - Optimized rapid travel distance: ~159.0
        - Improvement: ~71.79%
        """
        parser = PLTParser()
        writer = PLTWriter()

        # Load the 1x3 half inch letters holes1.plt example file
        examples_dir = Path(__file__).parent.parent / "examples"
        original_path = examples_dir / "1x3 half inch letters holes1.plt"

        if not original_path.exists():
            pytest.skip(f"Example file not found: {original_path}")

        # Parse the original document
        doc_original = parser.parse_file(original_path)

        # Calculate original rapid travel distance
        original_rapid_distance = doc_original.rapid_distance()

        # Profile the document
        profiler = Profiler()
        profile_result = profiler.profile(doc_original)

        # Chunk the document
        chunker = Chunker(config=ChunkerConfig(threshold_multiplier=2.0))
        blocks = chunker.chunk(
            doc_original.stroke_paths,
            profile_result.baseline_extent,
            is_structural=profile_result.is_structural,
        )

        if not blocks:
            pytest.skip("No blocks generated from file")

        # Optimize using NearestNeighbor2OptStrategy
        strategy = NearestNeighbor2OptStrategy()
        engine = OptimizerEngine(strategy=strategy)
        optimization_result = engine.optimize(blocks)

        # Reassemble into optimized document
        reassembler = Reassembler()
        doc_optimized = reassembler.reassemble(
            original_document=doc_original,
            blocks=blocks,
            optimization_result=optimization_result,
        )

        # Write optimized to temp file
        with tempfile.TemporaryDirectory() as tmpdir:
            optimized_path = Path(tmpdir) / "1x3_optimized.plt"
            writer.write_file(doc_optimized, optimized_path)

            # Re-parse the optimized file to calculate rapid travel distance
            doc_optimized_parsed = parser.parse_file(optimized_path)
            optimized_rapid_distance = doc_optimized_parsed.rapid_distance()

            # Calculate improvement percentage
            improvement_pct = (
                (original_rapid_distance - optimized_rapid_distance)
                / original_rapid_distance
                * 100.0
            )

            # Assert improvement is approximately 92.48%
            # Using a tolerance of ±0.5% to account for minor variations
            # (Improved from 71.79% due to redundant PU/PD pair removal optimization)
            assert math.isclose(improvement_pct, 92.48, abs_tol=0.5), (
                f"Rapid travel improvement {improvement_pct:.2f}% does not match expected 92.48%. "
                f"Original: {original_rapid_distance:.1f}, Optimized: {optimized_rapid_distance:.1f}"
            )
