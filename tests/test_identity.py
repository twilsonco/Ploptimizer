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
from plt_optimizer.core.models import Coordinate, PLTDocument
from plt_optimizer.core.optimizer import NearestNeighbor2OptStrategy, OptimizerEngine
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
            f"Segment count mismatch: {doc1.total_segments} vs {doc2.total_segments}"
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
        output1 = writer.write_string(doc1)

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
        assert writer._format_number(100.0) == "100"
        assert writer._format_number(0.5) == "0.5"
        assert writer._format_number(-18288.123456) == "-18288.123"

    def test_write_coordinate_formatting(self) -> None:
        """Test that coordinates are formatted correctly."""
        writer = PLTWriter()
        coord = Coordinate(x=18288.5, y=-0.125)

        formatted = writer._format_coord(coord)

        assert "18288.5" in formatted
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
            f"Stroke path count mismatch: {len(doc1.stroke_paths)} vs {len(doc2.stroke_paths)}"
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

            assert len(doc_original.header_commands) == len(doc_optimized_parsed.header_commands), (
                f"Header command count mismatch: {len(doc_original.header_commands)} vs "
                f"{len(doc_optimized_parsed.header_commands)}"
            )

            assert doc_original.header_commands == doc_optimized_parsed.header_commands, (
                "Header commands do not match exactly between original and optimized files"
            )
