"""Profiler module for baseline extent calculation.

This module analyzes parsed PLT documents to establish a baseline character/element
extent, which is used by the Chunker to determine stroke grouping thresholds.
The 95th percentile is used instead of maximum to avoid outlier sensitivity.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import List, Sequence

from plt_optimizer.core.models import (
    ArcSegment,
    Coordinate,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.utils.logging import get_text_logger


@dataclass(frozen=True)
class Extent:
    """Represents the width and height of a stroke's bounding box.

    Attributes:
        dx: Width (absolute difference in X coordinates).
        dy: Height (absolute difference in Y coordinates).
    """
    dx: float
    dy: float

    @property
    def max_dimension(self) -> float:
        """Return the larger of width or height."""
        result = max(self.dx, self.dy)
        return float(result)

    @property
    def euclidean_size(self) -> float:
        """Return the Euclidean norm (diagonal) of the extent."""
        dx = float(self.dx)
        dy = float(self.dy)
        return (dx * dx + dy * dy) ** 0.5


class ProfilerError(Exception):
    """Exception raised when profiling analysis fails.

    Attributes:
        message: Human-readable error description.
    """

    def __init__(self, message: str) -> None:
        """Initialize a ProfilerError.

        Args:
            message: Error description.
        """
        self.message = message
        super().__init__(message)


class Profiler:
    """Analyzer for calculating baseline extent from stroke paths.

    The profiler examines all cutting (pen-down) strokes in a PLTDocument and
    calculates the 95th percentile bounding box dimension. This value serves as
    the `baseline_extent` used by the Chunker to determine grouping thresholds,
    making it robust against outliers like underlines or borders.

    Example:
        >>> from plt_optimizer.core.parser import PLTParser
        >>> parser = PLTParser()
        >>> doc = parser.parse_string("IN;PU0,0;PD100,0;PD100,50;SP;")
        >>> profiler = Profiler()
        >>> result = profiler.profile(doc)
        >>> print(result.baseline_extent)
        102.345
    """

    def __init__(self) -> None:
        """Initialize the Profiler."""
        self._logger = get_text_logger()

    def profile(self, document: StrokePathsProtocol) -> ProfileResult:
        """Analyze a PLT document and calculate baseline extent.

        Args:
            document: A sequence of stroke paths to analyze (typically PLTDocument).

        Returns:
            A ProfileResult containing the baseline_extent and statistics.

        Raises:
            ProfilerError: If no cutting strokes are found.
        """
        self._logger.info("Starting baseline extent profiling")

        # Collect all extents from cutting segments
        extents = self._calculate_all_extents(document)

        if not extents:
            raise ProfilerError(
                "No cutting strokes found in document. Cannot calculate baseline extent."
            )

        # Calculate polyline density & structural composition
        valid_paths = [p for p in document.stroke_paths if p.segments]
        total_paths = len(valid_paths)
        total_segments = sum(len(p.segments) for p in valid_paths)

        avg_segments_per_path = total_segments / total_paths if total_paths > 0 else 0

        # Structural composition: Check if paths match structural fingerprints
        if total_paths > 0:
            structural_path_count = sum(
                1 for p in valid_paths if self._is_structural_path(p)
            )
            structural_ratio = structural_path_count / total_paths

            # If more than 90% of paths are purely structural features, flag as structural.
            # This handles files with drill holes (4x arcs + plunge) that have >3 segs/path.
            is_structural = structural_ratio > 0.9
        else:
            structural_path_count = 0
            structural_ratio = 0.0

        self._logger.debug(
            f"Polyline density analysis: {total_paths} paths, "
            f"{total_segments} total segments, "
            f"avg {avg_segments_per_path:.1f} segments/path, "
            f"structural={structural_path_count}/{total_paths} ({structural_ratio:.1%})"
        )

        # Calculate statistics
        dx_values = [e.dx for e in extents]
        dy_values = [e.dy for e in extents]

        median_dx = statistics.median(dx_values)
        median_dy = statistics.median(dy_values)

        # Use 95th percentile of max dimension for baseline_extent
        max_dimensions = [e.max_dimension for e in extents]
        max_dimensions_sorted = sorted(max_dimensions)
        p95_index = int(len(max_dimensions_sorted) * 0.95)
        if p95_index >= len(max_dimensions_sorted):
            p95_index = len(max_dimensions_sorted) - 1
        baseline_extent = max_dimensions_sorted[p95_index]

        result = ProfileResult(
            baseline_extent=baseline_extent,
            median_dx=median_dx,
            median_dy=median_dy,
            total_strokes=len(extents),
            p95_index=p95_index,
            is_structural=is_structural,
        )

        self._logger.info(
            f"Profiling complete: is_structural={is_structural} "
            f"(avg {avg_segments_per_path:.1f} segments/path, "
            f"{structural_ratio:.1%} structural), "
            f"baseline_extent={baseline_extent:.3f}, "
            f"total_cutting_strokes={result.total_strokes}"
        )

        return result

    def _calculate_all_extents(self, document: StrokePathsProtocol) -> List[Extent]:
        """Calculate bounding box extents for all cutting strokes.

        Args:
            document: Protocol supporting stroke_paths iteration.

        Returns:
            List of Extent objects for each cutting segment.
        """
        extents: List[Extent] = []

        for path in document.stroke_paths:
            for segment in path.segments:
                if not segment.is_cutting:
                    continue

                # Get coordinates based on segment type
                if isinstance(segment, StrokeSegment):
                    start = segment.start
                    end = segment.end
                else:
                    # For non-StrokeSegment types, skip arc segments initially
                    # The profiler handles pure linear strokes; arcs are rare in text
                    continue

                dx = abs(end.x - start.x)
                dy = abs(end.y - start.y)

                if dx > 0 or dy > 0:  # Ignore zero-length segments
                    extents.append(Extent(dx=dx, dy=dy))

        return extents

    def _is_structural_path(self, path: StrokePath) -> bool:
        """Determine if a single path is a structural feature (score line or drill hole).

        A structural path matches one of these patterns:
        1. Single straight StrokeSegment (simple score/cut line)
        2. EngraveLab drill hole: exactly 4x 90-degree arcs + optional zero-length plunge

        Args:
            path: The stroke path to classify.

        Returns:
            True if the path is a structural feature, False otherwise.
        """
        if not path.segments:
            return False

        # Check 1: Is it a single straight score line?
        if len(path.segments) == 1 and isinstance(path.segments[0], StrokeSegment):
            return True

        # Check 2: Is it an EngraveLab drill hole (4x 90-deg arcs + optional plunge)?
        arcs = [s for s in path.segments if isinstance(s, ArcSegment)]
        lines = [s for s in path.segments if isinstance(s, StrokeSegment)]

        if len(arcs) == 4 and all(abs(a.sweep_angle) == 90.0 for a in arcs):
            # Verify any straight lines are just zero-length plunge points
            if all(math.isclose(l.length, 0.0, abs_tol=1e-3) for l in lines):
                return True

        return False


@dataclass(frozen=True)
class ProfileResult:
    """Results from baseline extent profiling analysis.

    Attributes:
        baseline_extent: The 95th percentile of max bounding box dimension.
            Used as the threshold multiplier base in chunking.
        median_dx: Median width across all strokes.
        median_dy: Median height across all strokes.
        total_strokes: Number of cutting stroke segments analyzed.
        p95_index: Index into sorted dimensions that corresponds to 95th percentile.
        is_structural: True if the file contains structural features (drill holes,
            score lines, cutouts) instead of text. A file is classified as structural
            when more than 90%% of its paths match structural fingerprints:
            - Single straight StrokeSegment (score/cut line)
            - EngraveLab drill hole: exactly 4x 90-degree arcs + optional plunge
    """
    baseline_extent: float
    median_dx: float
    median_dy: float
    total_strokes: int
    p95_index: int
    is_structural: bool


# Protocol for type-safe access to stroke_paths without importing concrete types
from typing import Protocol

class StrokePathsProtocol(Protocol):
    """Protocol for objects that contain stroke path data.

    This protocol allows the Profiler to work with any object that provides
    an iterable `stroke_paths` attribute, such as PLTDocument.
    """

    @property
    def stroke_paths(self) -> Sequence[StrokePath]:
        """Return the sequence of stroke paths."""
        ...