"""Chunker module for grouping strokes into MacroBlocks.

This module implements chronological clustering of sequential stroke paths based
on tool-up (rapid) jump distances. The Chunker relies entirely on the original
chronological sequence to capture natural text entry order, avoiding any spatial
pre-sorting that would lose the user's intended flow.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from plt_optimizer.core.models import Coordinate, Segment, StrokePath
from plt_optimizer.utils.logging import get_text_logger


class ChunkerError(Exception):
    """Exception raised when chunking operation fails.

    Attributes:
        message: Human-readable error description.
    """

    def __init__(self, message: str) -> None:
        """Initialize a ChunkerError.

        Args:
            message: Error description.
        """
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class MacroBlock:
    """A group of consecutive stroke paths that represent a logical unit.

    A MacroBlock represents strokes that are grouped together because the tool-up
    (rapid) jumps between them are below the threshold, suggesting they belong to
    the same "word" or "line" in the original text entry sequence.

    For routing purposes, each block is abstracted by its:
    - Entrance: Start coordinate of the first stroke's first segment
    - Exit: End coordinate of the last stroke's last segment

    Attributes:
        block_id: Unique identifier for this block.
        paths: Tuple of StrokePaths belonging to this block (in original order).
        entrance: Coordinate where traversal of this block should begin.
        exit: Coordinate where traversal of this block ends.
    """

    block_id: int
    paths: tuple[StrokePath, ...]
    entrance: Coordinate
    exit: Coordinate

    @property
    def path_count(self) -> int:
        """Return the number of stroke paths in this block."""
        return len(self.paths)

    @property
    def total_segment_count(self) -> int:
        """Return total number of segments across all paths."""
        return sum(len(p.segments) for p in self.paths)


@dataclass
class ChunkerConfig:
    """Configuration parameters for the chunking algorithm.

    Attributes:
        threshold_multiplier: Multiplier applied to baseline_extent to determine
            the jump threshold. Default is 1.5x.
        min_block_size: Minimum number of paths required to form a valid block.
            Blocks smaller than this are merged with neighbors. Default is 1.
        enable_intra_chunk_optimization: Whether to optimize stroke path order and
            direction within each block. When enabled, intra-chunk optimization runs
            before inter-chunk routing to reduce internal rapid travel. Default is True.
        same_row_preference: Penalty multiplier for y-differences when computing
            jump distance between strokes. Values > 1.0 increase the effective cost
            of vertical jumps, biasing grouping toward strokes on the same horizontal
            line. Default is 1.0 (no penalty, backward compatible).
    """

    threshold_multiplier: float = 1.5
    min_block_size: int = 1
    enable_intra_chunk_optimization: bool = True
    same_row_preference: float = 1.0


class Chunker:
    """Groups sequential strokes into MacroBlocks based on jump distance.

    The Chunker iterates chronologically through stroke paths and groups consecutive
    paths together when the tool-up (rapid) jump between them is below a threshold.
    This threshold is calculated as `T * baseline_extent` where T is the tunable
    threshold_multiplier.

    The algorithm does NOT pre-sort spatially - it relies on chronological order
    to capture the user's natural text entry sequence. Isolated out-of-order
    additions will naturally form their own small blocks.

    Example:
        >>> from plt_optimizer.core.parser import PLTParser
        >>> from plt_optimizer.core.profiler import Profiler
        >>> parser = PLTParser()
        >>> doc = parser.parse_string("IN;PU0,0;PD100,0;PD200,0;SP;")
        >>> profiler = Profiler()
        >>> profile = profiler.profile(doc)
        >>> chunker = Chunker(config=ChunkerConfig(threshold_multiplier=1.5))
        >>> blocks = chunker.chunk(doc.stroke_paths, profile.baseline_extent)
    """

    def __init__(self, config: ChunkerConfig | None = None) -> None:
        """Initialize the Chunker.

        Args:
            config: Optional configuration parameters.
        """
        self._config = config or ChunkerConfig()
        self._logger = get_text_logger()

    def chunk(
        self,
        stroke_paths: Sequence[StrokePath],
        baseline_extent: float,
        is_structural: bool = False,
    ) -> list[MacroBlock]:
        """Group stroke paths into MacroBlocks based on jump distances.

        Args:
            stroke_paths: Chronologically ordered sequence of stroke paths.
            baseline_extent: The baseline extent from Profiler (95th percentile).
            is_structural: If True, bypass chronological chunking and create a 1:1
                mapping where every path becomes its own MacroBlock. This is used
                for structural files (drill holes, score lines) where each feature
                should be treated as an independent TSP node.

        Returns:
            List of MacroBlock objects in chronological order.

        Raises:
            ChunkerError: If no valid blocks can be created.
        """
        if not stroke_paths:
            raise ChunkerError("Cannot chunk empty sequence of stroke paths")

        # Initialize blocks list
        blocks: list[MacroBlock] = []

        # BYPASS: If structural, every path is its own independent block
        if is_structural:
            self._logger.info(
                f"Structural file detected: Bypassing chunker for 1:1 routing "
                f"({len([p for p in stroke_paths if p.segments])} paths)"
            )

            valid_paths = [p for p in stroke_paths if p.segments]
            for i, path in enumerate(valid_paths):
                first_seg = path.segments[0]
                last_seg = path.segments[-1]

                blocks.append(
                    MacroBlock(
                        block_id=i,
                        paths=(path,),
                        entrance=self._get_segment_start(first_seg),
                        exit=self._get_segment_end(last_seg),
                    )
                )
            return blocks

        jump_threshold = self._config.threshold_multiplier * baseline_extent

        self._logger.info(
            f"Chunking {len(stroke_paths)} stroke paths with "
            f"threshold={jump_threshold:.3f} (baseline_extent={baseline_extent:.3f}, "
            f"multiplier={self._config.threshold_multiplier})"
        )

        current_block_paths: list[StrokePath] = []

        for i, path in enumerate(stroke_paths):
            if not path.segments:
                continue

            # Calculate entrance for this path
            first_seg = path.segments[0]

            path_entrance = self._get_segment_start(first_seg)

            if not current_block_paths:
                # Start first block with this path
                current_block_paths.append(path)
                continue

            # Calculate weighted jump distance from previous path's exit to this path's entrance
            prev_path = current_block_paths[-1]
            prev_last_seg = prev_path.segments[-1]
            prev_exit = self._get_segment_end(prev_last_seg)

            dx = path_entrance.x - prev_exit.x
            dy = path_entrance.y - prev_exit.y
            base_jump_distance = math.sqrt(dx**2 + dy**2)
            y_penalty = (self._config.same_row_preference - 1.0) * abs(dy)
            jump_distance = base_jump_distance + y_penalty

            if jump_distance < jump_threshold:
                # Append to current block
                current_block_paths.append(path)
                self._logger.debug(
                    f"Path {i}: jump={jump_distance:.3f} < threshold={jump_threshold:.3f}, "
                    f"appending to current block (size now {len(current_block_paths)})"
                )
            else:
                # Seal current block and start new one
                if len(current_block_paths) >= self._config.min_block_size:
                    block = self._create_macro_block(blocks, current_block_paths)
                    blocks.append(block)

                current_block_paths = [path]
                self._logger.debug(
                    f"Path {i}: jump={jump_distance:.3f} >= threshold={jump_threshold:.3f}, "
                    f"starting new block"
                )

        # Don't forget the last block
        if current_block_paths and len(current_block_paths) >= self._config.min_block_size:
            block = self._create_macro_block(blocks, current_block_paths)
            blocks.append(block)

        if not blocks:
            raise ChunkerError(
                f"Could not create any valid blocks. "
                f"Consider adjusting threshold_multiplier (current={self._config.threshold_multiplier})"
            )

        self._logger.info(f"Created {len(blocks)} MacroBlocks")

        return blocks

    def _get_segment_start(self, segment: Segment) -> Coordinate:
        """Get the start coordinate of a stroke segment.

        Args:
            segment: The stroke segment (StrokeSegment or ArcSegment).

        Returns:
            Start coordinate.
        """
        return segment.start

    def _get_segment_end(self, segment: Segment) -> Coordinate:
        """Get the end coordinate of a stroke segment.

        Args:
            segment: The stroke segment (StrokeSegment or ArcSegment).

        Returns:
            End coordinate.
        """
        return segment.end

    def _create_macro_block(
        self,
        existing_blocks: list[MacroBlock],
        paths: list[StrokePath],
    ) -> MacroBlock:
        """Create a new MacroBlock from the given paths.

        Args:
            existing_blocks: Already-created blocks (for ID generation).
            paths: Stroke paths belonging to this block.

        Returns:
            A new MacroBlock instance.
        """
        block_id = len(existing_blocks)

        first_seg = paths[0].segments[0]
        last_seg = paths[-1].segments[-1]

        entrance = self._get_segment_start(first_seg)
        exit_coord = self._get_segment_end(last_seg)

        return MacroBlock(
            block_id=block_id,
            paths=tuple(paths),
            entrance=entrance,
            exit=exit_coord,
        )


class LinearChunker(Chunker):
    """Specialized Chunker for purely linear (1D) text files.

    This chunker extends the base Chunker with additional handling for
    1-dimensional text where strokes are primarily horizontal. It uses
    a tighter threshold calculation that accounts for the predominantly
    horizontal nature of text entry.
    """

    def __init__(self, config: ChunkerConfig | None = None) -> None:
        """Initialize the LinearChunker.

        Args:
            config: Optional configuration parameters with adjusted defaults.
        """
        # Use tighter threshold for linear (1D) text
        if config is None:
            config = ChunkerConfig(threshold_multiplier=1.2)
        super().__init__(config)
