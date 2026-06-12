"""PLT file writer for generating HPGL output.

This module provides functionality to convert structured PLTDocument objects
back into valid HPGL/PLT command strings, ensuring semantic equivalence and
mathematical precision (up to 3 decimal places) is preserved.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    FooterCommand,
    HeaderCommand,
    PenState,
    PLTDocument,
    Segment,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.core.parser import ParseError, PLTParser
from plt_optimizer.utils.logging import get_text_logger


class WriteError(Exception):
    """Exception raised when writing HPGL/PLT output fails.

    Attributes:
        message: Human-readable error description.
        document_part: Optional identifier of which part caused the error.
    """

    def __init__(
        self,
        message: str,
        document_part: Optional[str] = None,
    ) -> None:
        """Initialize a WriteError.

        Args:
            message: Error description.
            document_part: Identifier of problematic document section.
        """
        self.message = message
        self.document_part = document_part

        full_message = message
        if document_part is not None:
            full_message = f"{full_message} (in {document_part})"

        super().__init__(full_message)


class PLTWriter:
    """Writer for generating HPGL/PLT plotter files from structured documents.

    This writer converts PLTDocument objects into valid HPGL command strings
    suitable for output to physical plotters. It preserves precision to 3
    decimal places and maintains the exact command sequence required by
    the EngraveLab format.

    Example:
        >>> doc = PLTDocument()
        >>> doc.header_commands.append(HeaderCommand("IN"))
        >>> writer = PLTWriter()
        >>> output = writer.write_string(doc)
        >>> print(output)
        IN;SP;
    """

    # Coordinate precision (3 decimal places)
    COORD_PRECISION = 3

    def __init__(self) -> None:
        """Initialize the PLT writer."""
        self._logger = get_text_logger()

    def write_file(
        self,
        document: PLTDocument,
        file_path: Path,
        add_bom: bool = False,
    ) -> None:
        """Write a PLTDocument to a file.

        Args:
            document: The structured document to write.
            file_path: Destination path for the .plt file.
            add_bom: Whether to add UTF-8 BOM (for Windows compatibility).

        Raises:
            WriteError: If the file cannot be written.
        """
        self._logger.info(f"Writing PLT file: {file_path}")

        content = self.write_string(document)

        try:
            # Ensure parent directory exists
            file_path.parent.mkdir(parents=True, exist_ok=True)

            if add_bom:
                # Add UTF-8 BOM for Windows compatibility with some applications
                file_path.write_bytes(
                    "\ufeff".encode("utf-8") + content.encode("utf-8")
                )
            else:
                file_path.write_text(content, encoding="utf-8")

        except OSError as e:
            raise WriteError(f"Failed to write file: {e}") from e

    def write_string(self, document: PLTDocument) -> str:
        """Convert a PLTDocument to an HPGL command string.

        Args:
            document: The structured document to convert.

        Returns:
            Formatted HPGL/PLT command string.
        """
        parts: List[str] = []

        # Write header commands in sequence
        for header in document.header_commands:
            formatted = self._format_header(header)
            if formatted:
                parts.append(formatted)

        # Write stroke paths (PU/PD sequences)
        for path in document.stroke_paths:
            path_str = self._format_stroke_path(path)
            if path_str:
                parts.append(path_str)

        # Write footer commands
        for footer in document.footer_commands:
            formatted = self._format_footer(footer)
            if formatted:
                parts.append(formatted)

        result = "".join(parts)

        self._logger.debug(f"Generated {len(result)} characters of PLT output")
        return result

    def _format_header(self, header: HeaderCommand) -> str:
        """Format a header command as an HPGL string.

        Args:
            header: The header command to format.

        Returns:
            Formatted command string (e.g., 'VS0.50;').
        """
        if header.parameters is None:
            return f"{header.instruction};"

        param_str = ",".join(
            self._format_number(p) for p in header.parameters
        )
        return f"{header.instruction}{param_str};"

    def _format_footer(self, footer: FooterCommand) -> str:
        """Format a footer command as an HPGL string.

        Args:
            footer: The footer command to format.

        Returns:
            Formatted command string (e.g., 'SP;').
        """
        return f"{footer.instruction};"

    def _format_stroke_path(self, path: StrokePath) -> str:
        """Format a stroke path as PU/PD commands.

        Args:
            path: The stroke path to format.

        Returns:
            Formatted command string with coordinates.
        """
        if path.is_empty:
            return ""

        parts: List[str] = []

        if path.pen_up_position is not None:
            pos = path.pen_up_position
            parts.append(f"PU{self._format_coord(pos)};")
        elif path.segments and path.segments[0].is_cutting:
            first_seg = path.segments[0]
            parts.append(f"PU{self._format_coord(first_seg.start)};")

        for segment in path.segments:
            if isinstance(segment, ArcSegment):
                arc_str = self._format_arc_segment(segment)
                parts.append(arc_str)
            else:
                cmd = "PD" if segment.is_cutting else "PU"
                parts.append(f"{cmd}{self._format_coord(segment.end)};")

        return "".join(parts)

    def _format_arc_segment(self, arc: ArcSegment) -> str:
        """Format an arc segment as PD;AA or PU;AA command.

        Args:
            arc: The arc segment to format.

        Returns:
            Formatted command string (e.g., 'PD;AA1016.000,1016.000,-90.000;').
        """
        cmd = "PD" if arc.is_cutting else "PU"
        cx = self._format_number(arc.center.x)
        cy = self._format_number(arc.center.y)
        angle = self._format_number(arc.sweep_angle)
        return f"{cmd};AA{cx},{cy},{angle};"

    def _format_coord(self, coord: Coordinate) -> str:
        """Format a coordinate pair for HPGL output.

        Args:
            coord: The coordinate to format.

        Returns:
            Formatted coordinate string (e.g., '18288.000,0.000').
        """
        x = self._format_number(coord.x)
        y = self._format_number(coord.y)
        return f"{x},{y}"

    def _format_number(self, value: float) -> str:
        """Format a numeric value to 3 decimal places.

        Args:
            value: The numeric value to format.

        Returns:
            String representation with exactly 3 decimal places.
        """
        # Use fixed-point formatting to ensure consistent precision
        return f"{value:.3f}"

    def validate_output(
        self,
        original: PLTDocument,
        output: str,
    ) -> Tuple[bool, List[str]]:
        """Validate that generated output matches the original document.

        This method performs a round-trip validation by:
        1. Re-parsing the output string
        2. Comparing key properties (segment count, coordinate precision)

        Args:
            original: The original parsed document.
            output: The generated HPGL string.

        Returns:
            Tuple of (is_valid, list_of_error_messages).
        """
        errors: List[str] = []

        try:
            # Re-parse the output
            reparsed_parser = PLTParser()
            reparsed = reparsed_parser.parse_string(output)

            # Compare basic properties
            orig_segment_count = original.total_segments
            reparse_segment_count = reparsed.total_segments

            if orig_segment_count != reparse_segment_count:
                errors.append(
                    f"Segment count mismatch: {orig_segment_count} vs "
                    f"{reparse_segment_count}"
                )

            # Compare distances (should be mathematically equivalent)
            orig_distance = original.cutting_distance()
            reparse_distance = reparsed.cutting_distance()

            import math
            if not math.isclose(orig_distance, reparse_distance, rel_tol=1e-3):
                errors.append(
                    f"Distance mismatch: {orig_distance:.3f} vs "
                    f"{reparse_distance:.3f}"
                )

        except ParseError as e:
            errors.append(f"Re-parsing failed: {e}")

        return (len(errors) == 0, errors)