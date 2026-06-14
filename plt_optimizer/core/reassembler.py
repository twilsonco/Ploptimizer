"""Re-assembler module for reconstructing optimized PLT documents.

This module translates optimized block sequences back into raw Stroke objects,
handling necessary internal stroke reversals when a MacroBlock must be traversed
in reverse order. It produces a new PLTDocument suitable for writing.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import List, Optional, Tuple

from plt_optimizer.core.chunker import MacroBlock
from plt_optimizer.core.intra_chunk_optimizer import IntraChunkResult, PathTraverseState
from plt_optimizer.core.models import (
    _segment_length,
    ArcSegment,
    Coordinate,
    PenState,
    PLTDocument,
    Segment,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.core.optimizer import BlockTraverseState, OptimizationResult
from plt_optimizer.utils.logging import get_text_logger


class ReassemblerError(Exception):
    """Exception raised when reassembly fails.

    Attributes:
        message: Human-readable error description.
    """

    def __init__(self, message: str) -> None:
        """Initialize a ReassemblerError.

        Args:
            message: Error description.
        """
        self.message = message
        super().__init__(message)


class Reassembler:
    """Reconstructs PLTDocument from optimized MacroBlock traversal order.

    The Reassembler takes an OptimizationResult and the original document,
    producing a new PLTDocument where blocks are in the optimized sequence.
    When a block's BlockTraverseState indicates reversed=true, the Reassembler
    handles the reversal by:
    1. Reversing the sequence of StrokePath objects within that block
    2. Swapping start and end coordinates of every individual StrokeSegment

    Example:
        >>> from plt_optimizer.core.chunker import Chunker
        >>> from plt_optimizer.core.optimizer import OptimizerEngine
        >>>
        >>> chunker = Chunker()
        >>> blocks = chunker.chunk(doc.stroke_paths, baseline_extent)
        >>>
        >>> engine = OptimizerEngine(strategy=NearestNeighbor2OptStrategy())
        >>> result = engine.optimize(blocks)
        >>>
        >>> reassembler = Reassembler()
        >>> optimized_doc = reassembler.reassemble(doc, blocks, result)
    """

    def __init__(self) -> None:
        """Initialize the Reassembler."""
        self._logger = get_text_logger()

    def reassemble(
        self,
        original_document: PLTDocument,
        blocks: List[MacroBlock],
        optimization_result: OptimizationResult,
        intra_chunk_results: Optional[List[IntraChunkResult]] = None,
    ) -> PLTDocument:
        """Reconstruct an optimized PLTDocument from MacroBlocks.

        Args:
            original_document: The original parsed document (for headers/footers).
            blocks: All MacroBlocks that were optimized.
            optimization_result: Result containing optimal traversal order.
            intra_chunk_results: Optional list of IntraChunkResult, one per block
                in original block order. If provided, paths within each block are
                reordered/reversed according to the intra-chunk optimization.

        Returns:
            A new PLTDocument with strokes in optimized sequence.

        Raises:
            ReassemblerError: If reassembly cannot be completed.
        """
        self._logger.info(
            f"Reassembling document with {optimization_result.block_count} blocks"
        )

        # Build map of block_id to MacroBlock for quick lookup
        block_map = {block.block_id: block for block in blocks}

        # Build intra-chunk result map if provided
        intra_map: dict[int, IntraChunkResult] = {}
        if intra_chunk_results is not None:
            for i, b in enumerate(blocks):
                if i < len(intra_chunk_results):
                    intra_map[b.block_id] = intra_chunk_results[i]

        # Reconstruct optimized stroke paths
        optimized_paths: List[StrokePath] = []

        for traverse_state in optimization_result.traverse_order:
            maybe_block = block_map.get(traverse_state.block_id)
            if maybe_block is None:
                raise ReassemblerError(
                    f"Block {traverse_state.block_id} not found in chunked blocks"
                )
            current_block: MacroBlock = maybe_block

            # Get intra-chunk result if available
            intra_result = intra_map.get(current_block.block_id)

            # Process paths based on whether this block should be reversed
            if traverse_state.reversed:
                processed_paths = self._reverse_block_paths(
                    current_block.paths, intra_result
                )
            else:
                processed_paths = self._apply_intra_chunk_order(
                    current_block.paths, intra_result
                )

            optimized_paths.extend(processed_paths)

        # Build new document preserving headers and footers
        result_doc = PLTDocument(
            header_commands=original_document.header_commands.copy(),
            stroke_paths=optimized_paths,
            footer_commands=original_document.footer_commands.copy(),
        )

        self._logger.info(f"Reassembly complete: {len(optimized_paths)} paths")

        return result_doc

    def _apply_intra_chunk_order(
        self,
        paths: Tuple[StrokePath, ...],
        intra_result: Optional[IntraChunkResult],
    ) -> List[StrokePath]:
        """Apply intra-chunk optimized order without reversing entire block.

        Args:
            paths: Original tuple of stroke paths.
            intra_result: Intra-chunk optimization result or None.

        Returns:
            List of stroke paths in intra-chunk optimized order/direction.
        """
        if intra_result is None:
            return list(paths)

        reordered_paths: List[StrokePath] = []

        for path_state in intra_result.traverse_order:
            original_path = paths[path_state.path_index]
            if not original_path.segments:
                continue

            if path_state.reversed:
                reversed_segments = self._reverse_segment_order(original_path)
                new_pen_up: Optional[Coordinate] = (
                    reversed_segments[0].start if reversed_segments
                    else original_path.pen_up_position
                )
                reordered_paths.append(StrokePath(
                    pen_up_position=new_pen_up,
                    segments=tuple(reversed_segments),
                ))
            else:
                reordered_paths.append(original_path)

        return reordered_paths

    def _reverse_block_paths(
        self,
        paths: Tuple[StrokePath, ...],
        intra_result: Optional[IntraChunkResult] = None,
    ) -> List[StrokePath]:
        """Reverse the order of paths and all segments within each path.

        When a block must be traversed in reverse (right-to-left), we need to:
        1. Reverse the sequence of StrokePaths within the block
        2. For each StrokePath, reverse the segment order if not already optimized

        Args:
            paths: Original tuple of stroke paths.
            intra_result: Optional intra-chunk result for direction info.

        Returns:
            List of reversed and transformed stroke paths.
        """
        if intra_result is None:
            return self._reverse_paths_simple(paths)

        reordered = self._apply_intra_chunk_order(paths, intra_result)
        return list(reversed(reordered))

    def _reverse_segment_order(self, path: StrokePath) -> List[Segment]:
        """Reverse the order of segments within a path and swap coordinates.

        Args:
            path: Original stroke path.

        Returns:
            List of segments in reverse order with swapped start/end.
        """
        new_segments: List[Segment] = []

        for segment in reversed(path.segments):
            if isinstance(segment, ArcSegment):
                new_segment: Segment = ArcSegment(
                    start=segment.end,
                    end=segment.start,
                    center=segment.center,
                    sweep_angle=-segment.sweep_angle,
                    is_cutting=segment.is_cutting,
                )
            else:
                new_segment = StrokeSegment(
                    start=segment.end,
                    end=segment.start,
                    is_cutting=segment.is_cutting,
                )
            new_segments.append(new_segment)

        return new_segments

    def _reverse_paths_simple(self, paths: Tuple[StrokePath, ...]) -> List[StrokePath]:
        """Simple path reversal without intra-chunk optimization.

        Args:
            paths: Original tuple of stroke paths.

        Returns:
            List of reversed and transformed stroke paths.
        """
        reversed_paths: List[StrokePath] = []

        for path in reversed(paths):
            if not path.segments:
                continue

            new_segments = self._reverse_segment_order(path)

            new_pen_up: Optional[Coordinate] = (
                new_segments[0].start if new_segments else path.pen_up_position
            )

            reversed_paths.append(StrokePath(
                pen_up_position=new_pen_up,
                segments=tuple(new_segments),
            ))

        return reversed_paths

    def _reverse_segment(self, segment: StrokeSegment) -> StrokeSegment:
        """Create a new segment with swapped start and end coordinates.

        Args:
            segment: Original segment to reverse.

        Returns:
            New segment with opposite direction.
        """
        return StrokeSegment(
            start=segment.end,
            end=segment.start,
            is_cutting=segment.is_cutting,
        )

    def calculate_reversal_cost(self, block: MacroBlock) -> float:
        """Calculate the cost impact of reversing a block.

        This is used by optimization strategies to evaluate whether
        entering from the exit (and traversing backward) is beneficial.

        Args:
            block: The MacroBlock to evaluate.

        Returns:
            Delta cost if block were reversed. Negative means reversal saves distance.
        """
        # Cost to enter at entrance and traverse forward vs
        # cost to enter at exit and traverse backward

        # For simplicity, assume we're already positioned near the entrance
        # The difference is roughly the internal path length
        internal_length = 0.0
        for path in block.paths:
            for segment in path.segments:
                internal_length += _segment_length(segment)

        return 0.0  # Reversal doesn't change travel distance, only internal order


class MetricsCalculator:
    """Calculates optimization metrics for logging and analysis.

    This utility class computes before/after statistics to measure
    the effectiveness of the optimization.
    """

    def __init__(self) -> None:
        """Initialize the MetricsCalculator."""
        self._logger = get_text_logger()

    def calculate_original_travel_distance(
        self,
        document: PLTDocument,
    ) -> float:
        """Calculate total tool-up travel distance in original document.

        Args:
            document: The parsed PLTDocument.

        Returns:
            Total rapid (pen-up) movement distance.
        """
        return document.rapid_distance()

    def calculate_optimized_travel_distance(
        self,
        optimization_result: OptimizationResult,
    ) -> float:
        """Calculate total tool-up travel distance after optimization.

        Args:
            optimization_result: Result from optimizer.

        Returns:
            Total rapid movement distance based on block connections.
        """
        return optimization_result.total_travel_distance

    def calculate_improvement(
        self,
        original_distance: float,
        optimized_distance: float,
    ) -> Tuple[float, float]:
        """Calculate improvement metrics between original and optimized.

        Args:
            original_distance: Travel distance before optimization.
            optimized_distance: Travel distance after optimization.

        Returns:
            Tuple of (absolute_savings, percent_improvement).
        """
        absolute_savings = original_distance - optimized_distance

        if original_distance > 0:
            pct_improvement = (absolute_savings / original_distance) * 100
        else:
            pct_improvement = 0.0

        return (absolute_savings, pct_improvement)

    def log_metrics(
        self,
        job_id: str,
        original_file: Optional[str],
        optimized_doc: Optional[PLTDocument],
        optimization_result: OptimizationResult,
        status: str,
    ) -> None:
        """Log metrics to the CSV metrics logger.

        Args:
            job_id: Unique identifier for this job.
            original_file: Path to input file (if available).
            optimized_doc: The optimized output document (if successful).
            optimization_result: Result from optimizer.
            status: Job completion status ('success', 'failed', 'skipped').
        """
        from plt_optimizer.utils.logging import get_metrics_logger

        metrics_logger = get_metrics_logger()

        original_distance = 0.0
        if optimized_doc is not None:
            original_distance = self.calculate_original_travel_distance(optimized_doc)

        optimized_distance = optimization_result.total_travel_distance

        # Calculate file paths (if available)
        from pathlib import Path
        orig_path = Path(original_file) if original_file else None
        opt_path: Optional[Path] = None

        metrics_logger.log_job(
            job_id=job_id,
            original_file=orig_path or Path("unknown"),
            optimized_file=opt_path,
            original_distance=original_distance,
            optimized_distance=optimized_distance,
            status=status,
        )