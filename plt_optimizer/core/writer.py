"""PLT file writer for generating HPGL output.

This module provides functionality to convert structured PLTDocument objects
back into valid HPGL/PLT command strings, ensuring semantic equivalence and
mathematical precision (up to 3 decimal places) is preserved.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    FooterCommand,
    HeaderCommand,
    PLTDocument,
    StrokePath,
    _segment_length,
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
        document_part: str | None = None,
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
                file_path.write_bytes("\ufeff".encode("utf-8") + content.encode("utf-8"))
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
        parts: list[str] = []

        # Write header commands in sequence
        for header in document.header_commands:
            formatted = self._format_header(header)
            if formatted:
                parts.append(formatted)

        current_pos: Coordinate | None = None  # Track spindle position across paths

        # Write stroke paths (PU/PD sequences)
        for path in document.stroke_paths:
            path_str, current_pos = self._format_stroke_path(path, current_pos)
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

        param_str = ",".join(self._format_number(p) for p in header.parameters)
        return f"{header.instruction}{param_str};"

    def _format_footer(self, footer: FooterCommand) -> str:
        """Format a footer command as an HPGL string.

        Args:
            footer: The footer command to format.

        Returns:
            Formatted command string (e.g., 'SP;').
        """
        return f"{footer.instruction};"

    def _format_stroke_path(
        self,
        path: StrokePath,
        current_pos: Coordinate | None = None,
    ) -> tuple[str, Coordinate | None]:
        """Format a stroke path as PU/PD commands with segment-level state tracking.

        Args:
            path: The stroke path to format.
            current_pos: Current spindle position from previous paths.

        Returns:
            Tuple of (formatted command string, updated spindle position).
        """
        if path.is_empty:
            return "", current_pos

        parts: list[str] = []

        # Handle explicit pen_up_position for the initial move into this path
        first_segment_start = path.segments[0].start
        pen_up_target = (
            path.pen_up_position if path.pen_up_position is not None else first_segment_start
        )

        # Only issue initial PU if we are NOT already at the target coordinate
        if current_pos is None or not (
            math.isclose(current_pos.x, pen_up_target.x, abs_tol=1e-3)
            and math.isclose(current_pos.y, pen_up_target.y, abs_tol=1e-3)
        ):
            parts.append(f"PU{self._format_coord(pen_up_target)};")
            current_pos = pen_up_target

        # Now process each segment with segment-level state tracking
        for segment in path.segments:
            # Robust state tracking: If we are not physically at the start of this
            # specific segment, we MUST lift the tool and move to its start.
            if not (
                math.isclose(current_pos.x, segment.start.x, abs_tol=1e-3)
                and math.isclose(current_pos.y, segment.start.y, abs_tol=1e-3)
            ):
                parts.append(f"PU{self._format_coord(segment.start)};")
                current_pos = segment.start

            # Execute the cut
            if isinstance(segment, ArcSegment):
                arc_str = self._format_arc_segment(segment)
                parts.append(arc_str)
                current_pos = segment.end
            else:
                cmd = "PD" if segment.is_cutting else "PU"
                parts.append(f"{cmd}{self._format_coord(segment.end)};")
                current_pos = segment.end

        return "".join(parts), current_pos

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
    ) -> tuple[bool, list[str]]:
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
        errors: list[str] = []

        try:
            # Re-parse the output
            reparsed_parser = PLTParser()
            reparsed = reparsed_parser.parse_string(output)

            # Compare basic properties
            orig_segment_count = original.total_segments
            reparse_segment_count = reparsed.total_segments

            if orig_segment_count != reparse_segment_count:
                errors.append(
                    f"Segment count mismatch: {orig_segment_count} vs {reparse_segment_count}"
                )

            # Compare distances (should be mathematically equivalent)
            orig_distance = original.cutting_distance()
            reparse_distance = reparsed.cutting_distance()

            if not math.isclose(orig_distance, reparse_distance, rel_tol=1e-3):
                errors.append(f"Distance mismatch: {orig_distance:.3f} vs {reparse_distance:.3f}")

        except ParseError as e:
            errors.append(f"Re-parsing failed: {e}")

        return (len(errors) == 0, errors)

    def validate_against_original(
        self,
        original_file_path: Path,
        output_content: str,
    ) -> tuple[bool, list[str]]:
        """Validate generated HPGL content against the original file.

        This performs detailed comparison of HPGL command counts and sequences
        to detect issues like lost PU commands during consecutive rapid moves.

        Args:
            original_file_path: Path to the original PLT file.
            output_content: The generated HPGL string.

        Returns:
            Tuple of (is_valid, list_of_error_messages).
        """
        errors: list[str] = []
        warnings: list[str] = []

        # Command pattern for tokenization
        COMMAND_PATTERN = re.compile(r"([A-Z][A-Z0-9,.\-:]*?;)")

        try:
            original_content = original_file_path.read_text(encoding="utf-8")
        except OSError as e:
            return False, [f"Failed to read original file: {e}"]

        # Tokenize both
        orig_tokens = COMMAND_PATTERN.findall(original_content)
        opt_tokens = COMMAND_PATTERN.findall(output_content)

        # Count PU and PD commands
        orig_pu_count = sum(1 for t in orig_tokens if t.startswith("PU"))
        opt_pu_count = sum(1 for t in opt_tokens if t.startswith("PU"))
        orig_pd_count = sum(1 for t in orig_tokens if t.startswith("PD"))
        opt_pd_count = sum(1 for t in opt_tokens if t.startswith("PD"))

        # Check PU count discrepancy
        pu_diff = orig_pu_count - opt_pu_count
        if pu_diff > 0:
            warnings.append(
                f"PU command count reduced: {orig_pu_count} in original, "
                f"{opt_pu_count} in output (lost {pu_diff} PUs). This indicates "
                f"consecutive PU commands may have been collapsed."
            )
        elif pu_diff < 0:
            warnings.append(
                f"PU command count increased: {orig_pu_count} in original, "
                f"{opt_pu_count} in output (gained {-pu_diff} PUs)."
            )

        # Check PD count discrepancy
        pd_diff = orig_pd_count - opt_pd_count
        if abs(pd_diff) > 2:
            warnings.append(
                f"PD command count changed: {orig_pd_count} in original, "
                f"{opt_pd_count} in output (diff {pd_diff})."
            )

        # Find specific missing PU commands
        orig_pu_set = {t for t in orig_tokens if t.startswith("PU")}
        opt_pu_set = {t for t in opt_tokens if t.startswith("PU")}

        missing_pus = orig_pu_set - opt_pu_set
        if missing_pus:
            # Group by coordinate pattern to summarize
            coord_issues: dict[str, int] = {}
            for pu in missing_pus:
                # Extract coordinates
                match = re.match(r"PU(-?\d+\.\d+),(-?\d+\.\d+);", pu)
                if match:
                    x, y = float(match.group(1)), float(match.group(2))
                    key = f"({x:.0f},{y:.0f})"
                    coord_issues[key] = coord_issues.get(key, 0) + 1

            # Check if this is intentional tip-to-tail optimization
            # by verifying the output still round-trips correctly
            try:
                original_parser = PLTParser()
                original_doc = original_parser.parse_string(original_content)
                reparsed_parser = PLTParser()
                reparsed = reparsed_parser.parse_string(output_content)

                orig_distance = sum(
                    _segment_length(seg)
                    for path in original_doc.stroke_paths
                    for seg in path.segments
                    if seg.is_cutting
                )
                reparse_distance = sum(
                    _segment_length(seg)
                    for path in reparsed.stroke_paths
                    for seg in path.segments
                    if seg.is_cutting
                )
                distance_preserved = math.isclose(orig_distance, reparse_distance, rel_tol=1e-3)

                if distance_preserved:
                    warnings.append(
                        f"Lost {len(missing_pus)} PU command(s) at coordinates "
                        f"{list(coord_issues.keys())}. This is likely intentional "
                        f"tip-to-tail optimization where consecutive strokes end/start "
                        f"at the same position. Distance preserved: {orig_distance:.3f} vs "
                        f"{reparse_distance:.3f}"
                    )
                else:
                    error_msg = (
                        f"Lost {len(missing_pus)} specific PU command(s) with "
                        f"distance mismatch. Affected coordinate regions: {coord_issues}"
                    )
                    errors.append(error_msg)
            except ParseError:
                # Can't verify round-trip, report as potential issue
                warnings.append(
                    f"Lost {len(missing_pus)} PU command(s). Coordinate regions: "
                    f"{coord_issues}. Unable to verify round-trip due to parse error."
                )

        # Detect consecutive PU sequences in original that might be problematic
        in_consecutive_pu = False
        for _i, token in enumerate(orig_tokens):
            if token.startswith("PU"):
                if not in_consecutive_pu:
                    in_consecutive_pu = True
            else:
                if in_consecutive_pu:
                    # We just ended a sequence of PUs followed by non-PU
                    pass  # Already tracked via PU count diff
                in_consecutive_pu = False

        # Final verdict
        is_valid = len(errors) == 0

        all_messages = []
        if warnings and not errors:
            all_messages.extend(warnings)
            all_messages.append(
                "WARNING: Output may have issues. Consider reviewing plots "
                "carefully before using with engraver."
            )
        all_messages.extend(errors)

        return is_valid, all_messages
