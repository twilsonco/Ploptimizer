"""Data models for representing HPGL/PLT plotter files.

This module provides immutable dataclasses for storing parsed PLT file structures,
preserving precision up to 3 decimal places as required by the EngraveLab format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Union


class PenState(Enum):
    """Represents whether the pen is currently down (cutting) or up (moving)."""
    UP = auto()
    DOWN = auto()


@dataclass(frozen=True)
class Coordinate:
    """A 2D coordinate with precision up to 3 decimal places.

    Attributes:
        x: X coordinate value.
        y: Y coordinate value.
    """
    x: float
    y: float

    def __post_init__(self) -> None:
        """Validate and round coordinates to 3 decimal places."""
        object.__setattr__(self, 'x', round(self.x, 3))
        object.__setattr__(self, 'y', round(self.y, 3))

    @classmethod
    def from_string(cls, x_str: str, y_str: str) -> Coordinate:
        """Parse coordinates from string representations.

        Args:
            x_str: String representation of X coordinate.
            y_str: String representation of Y coordinate.

        Returns:
            A new Coordinate instance with parsed values.
        """
        return cls(x=float(x_str), y=float(y_str))

    def distance_to(self, other: Coordinate) -> float:
        """Calculate Euclidean distance to another coordinate.

        Args:
            other: The target coordinate.

        Returns:
            Distance in plotter units (typically 0.001 inches or similar).
        """
        dx = self.x - other.x
        dy = self.y - other.y
        return (dx * dx + dy * dy) ** 0.5

    def as_tuple(self) -> tuple[float, float]:
        """Return coordinate as a tuple of (x, y)."""
        return (self.x, self.y)


@dataclass(frozen=True)
class HeaderCommand:
    """A header configuration command from the PLT file.

    Header commands set global state like pen speed, scaling, and coordinate modes.
    They must be preserved exactly in sequence during output.

    Attributes:
        instruction: The HPGL instruction mnemonic (e.g., 'IN', 'VS', 'ZO').
        parameters: Optional tuple of numeric parameters for the command.
    """
    instruction: str
    parameters: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        """Round any floating point parameters to 3 decimal places."""
        if self.parameters is not None:
            rounded = tuple(round(p, 3) for p in self.parameters)
            object.__setattr__(self, 'parameters', rounded)

    def format(self) -> str:
        """Format the command as a PLT string.

        Returns:
            Formatted command string (e.g., 'VS0.50;' or 'ZO123,1;').
        """
        if self.parameters is None:
            return f"{self.instruction};"
        param_str = ",".join(str(p) for p in self.parameters)
        return f"{self.instruction}{param_str};"

    @classmethod
    def from_token(cls, token: str) -> HeaderCommand:
        """Parse a command token into a HeaderCommand.

        Args:
            token: A PLT command token (e.g., 'VS0.50' or 'IN').

        Returns:
            A new HeaderCommand instance.
        """
        if not token.endswith(";"):
            raise ValueError(f"Invalid token format: {token}")
        token = token[:-1]  # Remove trailing ';'

        # Split instruction from parameters
        parts = token.split(":", 1)  # Some commands use : as separator
        if len(parts) == 2:
            instr, param_str = parts
            params = tuple(float(p) for p in param_str.split(","))
        else:
            # Try to split on letters followed by numbers
            import re
            match = re.match(r'^([A-Z]+)(.*)$', token)
            if match:
                instr = match.group(1)
                param_str = match.group(2)
                params: tuple[float, ...] | None = None
                if param_str:
                    params = tuple(float(p) for p in param_str.split(","))
            else:
                instr = token
                params = None

        return cls(instruction=instr, parameters=params)


@dataclass(frozen=True)
class StrokeSegment:
    """A single line segment in a stroke path.

    Attributes:
        start: Starting coordinate of the segment.
        end: Ending coordinate of the segment.
        is_cutting: True if pen was down during this segment (cutting).
    """
    start: Coordinate
    end: Coordinate
    is_cutting: bool

    @property
    def length(self) -> float:
        """Calculate the Euclidean length of this segment."""
        return self.start.distance_to(self.end)


@dataclass(frozen=True)
class ArcSegment:
    """An arc segment in a stroke path.

    Attributes:
        start: Starting coordinate of the arc.
        end: Ending coordinate of the arc (computed from center + angle).
        center: Center of the arc circle.
        sweep_angle: Sweep angle in degrees (+ = clockwise, - = counter-clockwise).
        is_cutting: True if pen was down during this segment.
    """
    start: Coordinate
    end: Coordinate
    center: Coordinate
    sweep_angle: float
    is_cutting: bool

    @property
    def radius(self) -> float:
        """Calculate the radius of the arc."""
        return self.start.distance_to(self.center)


Segment = Union[StrokeSegment, ArcSegment]


def _segment_length(seg: Segment) -> float:
    """Calculate the length of a segment (line or arc).

    For arcs, returns chord length (straight-line distance from start to end).
    """
    if isinstance(seg, ArcSegment):
        return seg.start.distance_to(seg.end)
    return seg.length


@dataclass(frozen=True)
class StrokePath:
    """A complete stroke path from a PU (pen up) to one or more PD (pen down) commands.

    A path begins with a PU command to move to a start position, optionally followed
    by one or more PD commands that define cutting segments. The sequence of alternating
    PU/PD commands defines rapid air travel vs actual cutting moves.

    Attributes:
        pen_up_position: Position after the initial pen-up move (or None if starts with PD).
        segments: Ordered tuple of stroke segments (line and/or arc).
    """
    pen_up_position: Coordinate | None = None
    segments: tuple[Segment, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        """Return True if this path has no segments."""
        return len(self.segments) == 0

    @property
    def total_distance(self) -> float:
        """Calculate the total Euclidean distance of all segments in this path.

        For ArcSegments, uses chord length (straight-line distance from start to end).
        For StrokeSegments, uses actual segment length.
        """
        return sum(_segment_length(seg) for seg in self.segments)

    @property
    def cutting_distance(self) -> float:
        """Calculate the total distance of cutting (pen down) segments only."""
        return sum(
            _segment_length(seg) for seg in self.segments if seg.is_cutting
        )

    @property
    def rapid_distance(self) -> float:
        """Calculate the total distance of rapid (pen up) moves only."""
        return sum(
            _segment_length(seg) for seg in self.segments if not seg.is_cutting
        )


@dataclass(frozen=True)
class FooterCommand:
    """A footer command that finalizes the PLT file.

    Attributes:
        instruction: The HPGL instruction mnemonic (e.g., 'SP', 'PG').
    """
    instruction: str

    def format(self) -> str:
        """Format the command as a PLT string."""
        return f"{self.instruction};"

    @classmethod
    def from_token(cls, token: str) -> FooterCommand:
        """Parse a command token into a FooterCommand.

        Args:
            token: A PLT command token (e.g., 'SP;').

        Returns:
            A new FooterCommand instance.
        """
        if not token.endswith(";"):
            raise ValueError(f"Invalid token format: {token}")
        token = token[:-1]  # Remove trailing ';'
        return cls(instruction=token)


@dataclass
class PLTDocument:
    """Complete representation of a parsed HPGL/PLT file.

    This dataclass contains all components of a PLT file:
    - Header commands (global configuration)
    - Stroke paths (the actual cutting/movement data)
    - Footer commands (finalization)

    Attributes:
        header_commands: Ordered list of header/configuration commands.
        stroke_paths: Ordered list of stroke paths.
        footer_commands: Ordered list of footer/finalization commands.
    """
    header_commands: list[HeaderCommand] = field(default_factory=list)
    stroke_paths: list[StrokePath] = field(default_factory=list)
    footer_commands: list[FooterCommand] = field(default_factory=list)

    def total_distance(self) -> float:
        """Calculate the sum of all cutting distances across all paths."""
        return sum(path.total_distance for path in self.stroke_paths)

    def cutting_distance(self) -> float:
        """Calculate the sum of all cutting segments across all paths."""
        return sum(path.cutting_distance for path in self.stroke_paths)

    def rapid_distance(self) -> float:
        """Calculate the sum of all rapid (pen-up) moves between stroke paths.

        Rapid travel is the distance the tool moves while pen is up, which occurs
        when moving from one path's end position to the next path's pen_up_position.
        """
        if len(self.stroke_paths) < 2:
            return 0.0

        total_rapid = 0.0
        for i in range(len(self.stroke_paths) - 1):
            curr_path = self.stroke_paths[i]
            next_path = self.stroke_paths[i + 1]

            if not curr_path.segments or next_path.pen_up_position is None:
                continue

            last_seg_end = curr_path.segments[-1].end
            rapid_dist = last_seg_end.distance_to(next_path.pen_up_position)
            total_rapid += rapid_dist

        return total_rapid

    @property
    def total_segments(self) -> int:
        """Return the total number of stroke segments across all paths."""
        return sum(len(path.segments) for path in self.stroke_paths)
