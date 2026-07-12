"""PLT file parser for HPGL plotter files.

This module provides tokenization and parsing functionality to convert
HPGL/PLT file content into structured PLTDocument data models suitable
for optimization processing.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from pathlib import Path
from typing import List, Optional, Tuple

from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    FooterCommand,
    HeaderCommand,
    PenState,
    PLTDocument,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.utils.logging import get_text_logger


class ParseError(Exception):
    """Exception raised when parsing HPGL/PLT file content fails.

    Attributes:
        message: Human-readable error description.
        line_number: Optional line number where the error occurred.
        token: Optional problematic token that triggered the error.
    """

    def __init__(
        self,
        message: str,
        line_number: Optional[int] = None,
        token: Optional[str] = None,
    ) -> None:
        """Initialize a ParseError.

        Args:
            message: Error description.
            line_number: Line number in source (if available).
            token: Problematic token (if identified).
        """
        self.message = message
        self.line_number = line_number
        self.token = token

        full_message = message
        if line_number is not None:
            full_message = f"{full_message} (line {line_number})"
        if token is not None:
            full_message = f"{full_message}: '{token}'"

        super().__init__(full_message)


class PLTParser:
    """Parser for HPGL/PLT plotter files from Cadlink EngraveLab.

    This parser converts raw HPGL command strings into structured PLTDocument
    objects. It handles header commands, stroke paths (PU/PD sequences),
    and footer commands while preserving precision to 3 decimal places.

    Example:
        >>> content = "IN;VS0.50;PA;PU0.000,0.000;PD18288.000,0.000;SP;"
        >>> parser = PLTParser()
        >>> doc = parser.parse_string(content)
        >>> len(doc.header_commands)
        3
    """

    # Pattern to split on semicolons (but keep the delimiter)
    COMMAND_PATTERN = re.compile(r"([A-Z][A-Z0-9,.\-:]*?;)")

    # Pattern to match coordinate pairs
    COORD_PATTERN = re.compile(r"^(-?\d+\.?\d*),(-?\d+\.?\d*)$")

    def __init__(self) -> None:
        """Initialize the PLT parser."""
        self._logger = get_text_logger()

    def parse_file(self, file_path: Path) -> PLTDocument:
        """Parse a PLT file and return a structured document.

        Args:
            file_path: Path to the .plt file.

        Returns:
            A PLTDocument containing all parsed data.

        Raises:
            ParseError: If the file cannot be read or parsed.
            FileNotFoundError: If the file does not exist.
        """
        self._logger.info(f"Parsing PLT file: {file_path}")

        try:
            # Read with universal newline support for cross-platform compatibility
            content = file_path.read_text(encoding="utf-8")
        except OSError as e:
            raise ParseError(f"Failed to read file: {e}") from e

        return self.parse_string(content)

    def parse_string(self, content: str) -> PLTDocument:
        """Parse HPGL command string content into a structured document.

        Args:
            content: Raw HPGL/PLT command string (may contain newlines).

        Returns:
            A PLTDocument containing all parsed data.

        Raises:
            ParseError: If the content cannot be parsed.
        """
        # Normalize line endings for consistent parsing
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        self._logger.debug(f"Parsing {len(normalized)} characters of PLT content")

        tokens = list(self._tokenize(normalized))
        return self._build_document(tokens)

    def _tokenize(self, content: str) -> Iterator[str]:
        """Split HPGL content into individual command tokens.

        Args:
            content: Normalized HPGL string.

        Yields:
            Individual command tokens with semicolons.
        """
        # Find all tokens matching the pattern
        for match in self.COMMAND_PATTERN.finditer(content):
            token = match.group(1)
            yield token

    def _build_document(self, tokens: List[str]) -> PLTDocument:
        """Build a PLTDocument from parsed tokens.

        Args:
            tokens: List of command tokens.

        Returns:
            Structured PLTDocument.
        """
        doc = PLTDocument()
        current_path: Optional[StrokePath] = None
        pen_state = PenState.UP
        last_position: Optional[Coordinate] = None

        i = 0
        while i < len(tokens):
            token = tokens[i]
            if not token or token == ";":
                i += 1
                continue

            cmd = token.rstrip(";")
            self._logger.debug(f"Processing command: {cmd}")

            arc_cmd_match = re.match(r"^(AA|AR|CI)(.*)$", cmd)
            if arc_cmd_match and last_position is not None:
                arc_type = arc_cmd_match.group(1)
                params_str = arc_cmd_match.group(2)

                arc_segment, end_pos = self._parse_arc_command(arc_type, params_str, last_position)
                if (
                    arc_segment is not None
                    and current_path is not None
                    and pen_state == PenState.DOWN
                ):
                    new_segments = current_path.segments + (arc_segment,)
                    object.__setattr__(current_path, "segments", new_segments)
                    last_position = end_pos

                i += 1
                continue

            if self._is_header_command(cmd):
                header = HeaderCommand.from_token(token)
                doc.header_commands.append(header)
                last_position = None
                i += 1
                continue

            elif cmd.startswith("PU") or cmd.startswith("PD"):
                new_pen_state = PenState.DOWN if cmd.startswith("PD") else PenState.UP
                coords, next_i = self._extract_coordinates(cmd, i, tokens)

                rest_after_pupd = cmd[2:] if len(cmd) > 2 else ""
                arc_in_same_token = re.match(r"^(AA|AR|CI)(.*)$", rest_after_pupd)

                for coord in coords:
                    if new_pen_state == PenState.DOWN and last_position is not None:
                        segment = StrokeSegment(
                            start=last_position,
                            end=coord,
                            is_cutting=True,
                        )

                        if pen_state == PenState.UP or current_path is None:
                            current_path = StrokePath(
                                pen_up_position=last_position,
                                segments=(segment,),
                            )
                            doc.stroke_paths.append(current_path)
                        else:
                            new_segments = current_path.segments + (segment,)
                            object.__setattr__(current_path, "segments", new_segments)

                    last_position = coord

                pen_state = new_pen_state
                i += 1

                if not coords and arc_in_same_token and last_position is not None:
                    arc_type = arc_in_same_token.group(1)
                    params_str = arc_in_same_token.group(2)

                    arc_segment, end_pos = self._parse_arc_command(
                        arc_type, params_str, last_position
                    )
                    if arc_segment is not None:
                        if pen_state == PenState.UP or current_path is None:
                            current_path = StrokePath(
                                pen_up_position=last_position,
                                segments=(arc_segment,),
                            )
                            doc.stroke_paths.append(current_path)
                        elif pen_state == PenState.DOWN:  # pragma: no branch
                            new_segments = current_path.segments + (arc_segment,)
                            object.__setattr__(current_path, "segments", new_segments)
                        last_position = end_pos

                elif (
                    not coords
                    and not arc_in_same_token
                    and last_position is not None
                    and i < len(tokens)
                ):
                    next_token = tokens[i].rstrip(";")
                    arc_match = re.match(r"^(AA|AR|CI)(.*)$", next_token)
                    if arc_match:
                        arc_type = arc_match.group(1)
                        params_str = arc_match.group(2)

                        arc_segment, end_pos = self._parse_arc_command(
                            arc_type, params_str, last_position
                        )
                        if arc_segment is not None:
                            if pen_state == PenState.UP or current_path is None:
                                current_path = StrokePath(
                                    pen_up_position=last_position,
                                    segments=(arc_segment,),
                                )
                                doc.stroke_paths.append(current_path)
                            elif pen_state == PenState.DOWN:  # pragma: no branch
                                new_segments = current_path.segments + (arc_segment,)
                                object.__setattr__(current_path, "segments", new_segments)
                            last_position = end_pos

                        i += 1

                continue

            elif cmd == "SP":
                footer = FooterCommand(instruction="SP")
                doc.footer_commands.append(footer)
                i += 1

            else:
                self._logger.warning(f"Unknown command '{cmd}', treating as header")
                try:
                    header = HeaderCommand.from_token(token)
                    doc.header_commands.append(header)
                except (ValueError, AttributeError) as e:
                    raise ParseError(
                        "Failed to parse command",
                        token=token,
                    ) from e
                i += 1

        return doc

    def _is_header_command(self, cmd: str) -> bool:
        """Check if a command is a header/configuration command.

        Args:
            cmd: Command string without trailing semicolon.

        Returns:
            True if this is a known header command.
        """
        # Commands that set state but don't draw
        header_commands = {
            "IN",  # Initialize
            "VS",  # Velocity Select
            "ZO",  # Zoom
            "VZ",  # Zoom (alternative)
            "ZU",  # Zoom (alternative)
            "PA",  # Plot Absolute
            "PR",  # Plot Relative
            "CS",  # Character Set
            "CA",  # Character Size Alternate
            "CC",  # Character Fill Gap
            "CI",  # Circle
            "DC",  # Digitize Clear
            "DF",  # Define Font
            "DI",  # Direction Input
            "DR",  # Draw Relative
            "DT",  # Digitize Type
            "EP",  # Edge Plot
            "ER",  # Error Reset
            "ES",  # Extra Space
            "ET",  # Edge Test
            "EW",  # Edge Width
            "FS",  # Force Select
            "FT",  # Fill Type
            "IM",  # Input Mask
            "IP",  # Input P1 and P2
            "IV",  # Invoke
            "IW",  # Input Window
            "KY",  # Key Definition
            "LO",  # Label Origin
            "LT",  # Linetype
            "NR",  # No Rotate
            "NT",  # No Terminator
            "OA",  # Output Actual Position
            "OC",  # Output Commanded Position
            "OD",  # Output Digitized Position
            "OE",  # Output Error
            "OG",  # Output Group
            "OH",  # Output P1 and P2
            "OI",  # Output Identified Position
            "OO",  # Output Options
            "OP",  # Output P1 and P2 (alternate)
            "OS",  # Output Status
            "OT",  # Output All Pallets
            "OW",  # Output Window
            "PT",  # Pen Thickness
            "QA",  # Quick Alpha
            "RA",  # Rectangle Absolute
            "RR",  # Rectangle Relative
            "SA",  # Select Alternate Font
            "SB",  # Standard Font
            "SC",  # Scale
            "SD",  # Select Digitize
            "SI",  # Size Absolute
            "SL",  # Slant
            "SM",  # Special Marker Mode
            "SN",  # Symbol Number
            "SS",  # Standard Selection
            "ST",  # Sort
            "SV",  # Screen Display
            "SW",  # Software Zoom
            "TD",  # Transparent Data
            "TH",  # Threshold
            "TL",  # Transparency
            "TM",  # Transform Mode
            "TR",  # Rotate
            "TS",  # Transparent Space
            "TV",  # Test Voltage
            "UC",  # User-defined Character
            "UL",  # User-defined Line-type
            "WD",  # Write Direct
            "WG",  # Fill Watermark Gradient
            "XT",  # X Tick
            "YT",  # Y Tick
            "ZA",  # Z Axis
            "ZW",  # Zoom Scale
        }

        # Extract just the instruction mnemonic (letters only at start)
        import re

        match = re.match(r"^([A-Z]+)", cmd)
        if not match:
            return False
        base_cmd = match.group(1)

        # PU and PD are handled specially as path commands, not headers
        if base_cmd in ("PU", "PD"):
            return False

        return base_cmd in header_commands or base_cmd in ("AA", "AR")

    def _parse_arc_command(
        self,
        arc_type: str,
        params_str: str,
        start_pos: Coordinate,
    ) -> Tuple[Optional[ArcSegment], Optional[Coordinate]]:
        """Parse an AA/AR/CI arc command and compute the end position.

        Args:
            arc_type: One of 'AA' (Arc Absolute), 'AR' (Arc Relative), or 'CI' (Circle).
            params_str: Parameter string after the command (e.g., '1016.000,1016.000,90.000').
            start_pos: Starting coordinate of the arc.

        Returns:
            Tuple of (ArcSegment, end_position) or (None, None) if parsing fails.
        """
        try:
            parts = params_str.split(",")

            cx: float
            cy: float
            sweep_angle: float

            if arc_type == "CI":
                radius = float(parts[0])
                sweep_angle = 360.0
                cx = start_pos.x
                cy = start_pos.y
                end_pos = start_pos
            else:
                if len(parts) < 3:
                    self._logger.warning(f"Arc command {arc_type} requires 3 parameters")
                    return None, None
                cx = float(parts[0])
                cy = float(parts[1])
                sweep_angle = float(parts[2])

                if arc_type == "AR":
                    sweep_angle = -sweep_angle

                radius = start_pos.distance_to(Coordinate(cx, cy))
                theta_start = math.atan2(start_pos.y - cy, start_pos.x - cx)
                delta_theta = sweep_angle * math.pi / 180
                end_x = cx + radius * math.cos(theta_start + delta_theta)
                end_y = cy + radius * math.sin(theta_start + delta_theta)

                end_pos = Coordinate(end_x, end_y)

            arc_segment = ArcSegment(
                start=start_pos,
                end=end_pos,
                center=Coordinate(cx, cy),
                sweep_angle=sweep_angle,
                is_cutting=True,
            )

            return arc_segment, end_pos

        except (ValueError, IndexError) as e:
            self._logger.warning(f"Failed to parse arc command {arc_type}: {e}")
            return None, None

    def _extract_coordinates(
        self,
        cmd: str,
        token_index: int,
        tokens: List[str],
    ) -> Tuple[List[Coordinate], int]:
        """Extract coordinate pairs from a command and subsequent tokens.

        Args:
            cmd: The current command (PU or PD).
            token_index: Index of the command in tokens list.
            tokens: Full list of tokens.

        Returns:
            Tuple of (list of Coordinates, index of next unprocessed token).
        """
        coords: List[Coordinate] = []
        i = token_index
        current_token = cmd

        # Extract coordinates from the remainder of the current token after PU/PD
        # Command prefix is always 2 characters (PU or PD)
        rest = current_token[2:] if len(current_token) > 2 else ""

        while True:
            # Try to match coordinates in current token's remainder
            if rest:
                coord_match = self.COORD_PATTERN.match(rest)
                if coord_match:
                    try:
                        coord = Coordinate.from_string(
                            coord_match.group(1),
                            coord_match.group(2),
                        )
                        coords.append(coord)
                    except ValueError as e:
                        raise ParseError(
                            "Invalid coordinate format",
                            token=rest,
                        ) from e
                    rest = rest[coord_match.end() :]
                    if rest.startswith(","):
                        rest = rest[1:]
                    continue

            # If no more coordinates in current token, look at next tokens
            # But only if we haven't already consumed something (to avoid infinite loop)
            i += 1
            if i >= len(tokens):
                break

            next_token = tokens[i].rstrip(";")
            if not next_token:
                break

            # Check if it's another command (not coordinates)
            coord_match = self.COORD_PATTERN.match(next_token)
            if not coord_match:
                # It's another command - don't consume it
                i -= 1  # Back up since we didn't use this token
                break

            try:
                coord = Coordinate.from_string(
                    coord_match.group(1),
                    coord_match.group(2),
                )
                coords.append(coord)
            except ValueError as e:
                raise ParseError(
                    "Invalid coordinate format",
                    token=next_token,
                ) from e

        return coords, i
