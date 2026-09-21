"""Profiler module for baseline extent calculation and structural classification.

This module analyzes parsed PLT documents to establish a baseline character/element
extent, which is used by the Chunker to determine stroke grouping thresholds.
The 95th percentile is used instead of maximum to avoid outlier sensitivity.

It also classifies a document as *structural* using a compositional rule: a
stroke path is structural when it consists exclusively of straight line
segments and verified circles (e.g. EngraveLab drill holes emitted as four
consecutive 90-degree arcs, or as 5-arc best-fit rings whose per-arc centers
wobble a few percent around one fitted circle). A document is structural when
the fraction of structural paths exceeds ``structural_ratio`` (default 85%).
Text paths fail the circle verification: EngraveLab renders glyph curves as
many tiny arcs whose centers scatter far beyond the fit tolerance, and
generated text is pure polylines with multi-segment glyph runs.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import List, Protocol

from plt_optimizer.core.models import (
    ArcSegment,
    Segment,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.utils.logging import get_text_logger

# Tolerance (plotter units) for verifying that a run of arcs chains into one
# circle. Covers the parser's 3-decimal Coordinate rounding plus the +/-0.001
# center jitter emitted by EngraveLab drill-hole arcs.
CIRCLE_TOLERANCE = 5e-3

# Angular tolerance (degrees) for accepting an arc group as a full revolution.
CIRCLE_SWEEP_TOLERANCE_DEG = 5.0

# Relative tolerance (fraction of the fitted radius) for accepting a run of
# jittered best-fit arcs as one circle. EngraveLab emits drill holes both as
# exact 4x90-degree arcs and as 5-arc best-fit rings whose per-arc centers
# wobble a few percent around the true circle (measured <=5% on shipped
# files); glyph outline loops deviate 20%+ and stay rejected.
CIRCLE_FIT_REL_TOLERANCE = 0.10

# Default document-level structural gate: a document is classified structural
# when the fraction of structural paths strictly exceeds this ratio.
DEFAULT_STRUCTURAL_RATIO = 0.85


def _arc_runs(segments: Sequence[Segment]) -> List[List[ArcSegment]]:
    """Group path segments into maximal consecutive arc runs.

    Straight segments act as run boundaries; a path with no arcs yields an
    empty list.

    Args:
        segments: Ordered segments of a single stroke path.

    Returns:
        List of runs, each a non-empty list of consecutive ArcSegments.
    """
    runs: List[List[ArcSegment]] = []
    current: List[ArcSegment] = []
    for seg in segments:
        if isinstance(seg, ArcSegment):
            current.append(seg)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _is_perfect_circle(run: Sequence[ArcSegment]) -> bool:
    """Verify that a consecutive arc run forms one circle.

    A run is a verified circle when:

    1. The signed sweep angles sum to +/-360 degrees (within
       ``CIRCLE_SWEEP_TOLERANCE_DEG``), so retraced back-and-forth arcs
       (e.g. +180/-180) are rejected, and
    2. for multi-arc runs, the arcs chain end-to-start and close back onto
       the first arc's start (within ``CIRCLE_TOLERANCE``), and
    3. the run *fits* one circle: every arc center lies within
       ``CIRCLE_FIT_REL_TOLERANCE`` of the mean arc center and every
       endpoint lies within that fraction of the fitted radius (mean
       distance from the mean center to the endpoints). Exact rings (all
       arcs sharing one center/radius) trivially pass; EngraveLab's
       best-fit-arc drill holes pass with a few percent of jitter, while
       glyph outline loops (centers scattered over ~60%+ of the radius)
       do not.

    A single arc whose own sweep is a full revolution (e.g. an HPGL ``CI``
    circle parsed as one 360-degree arc) is a circle by definition.

    Args:
        run: Maximal consecutive run of arc segments.

    Returns:
        True if the run is a verified full circle.
    """
    if not run:
        return False

    total_sweep = sum(arc.sweep_angle for arc in run)
    if not math.isclose(abs(total_sweep), 360.0, abs_tol=CIRCLE_SWEEP_TOLERANCE_DEG):
        return False

    if len(run) == 1:
        return True

    first = run[0]

    previous = first
    for arc in run[1:]:
        if not (
            math.isclose(previous.end.x, arc.start.x, abs_tol=CIRCLE_TOLERANCE)
            and math.isclose(previous.end.y, arc.start.y, abs_tol=CIRCLE_TOLERANCE)
        ):
            return False
        previous = arc

    if not (
        math.isclose(previous.end.x, first.start.x, abs_tol=CIRCLE_TOLERANCE)
        and math.isclose(previous.end.y, first.start.y, abs_tol=CIRCLE_TOLERANCE)
    ):
        return False

    # Fit one circle to the run: mean arc center, mean endpoint radius.
    center_x = sum(arc.center.x for arc in run) / len(run)
    center_y = sum(arc.center.y for arc in run) / len(run)
    endpoints = [arc.start for arc in run] + [arc.end for arc in run]
    radii = [math.hypot(point.x - center_x, point.y - center_y) for point in endpoints]
    fit_radius = sum(radii) / len(radii)
    if fit_radius <= 0.0:
        return False

    tolerance = CIRCLE_FIT_REL_TOLERANCE * fit_radius
    for arc in run:
        if abs(arc.center.x - center_x) > tolerance or abs(arc.center.y - center_y) > tolerance:
            return False
    for radius in radii:
        if abs(radius - fit_radius) > tolerance:
            return False

    return True


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
        return math.sqrt(dx * dx + dy * dy)


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
    """Analyzer for baseline extent calculation and structural classification.

    The profiler examines all cutting (pen-down) strokes in a PLTDocument and
    calculates the 95th percentile bounding box dimension. This value serves as
    the `baseline_extent` used by the Chunker to determine grouping thresholds,
    making it robust against outliers like underlines or borders. Only straight
    line segments contribute to the baseline: drill holes (circles) are excluded
    by design so hole size never skews the text-grouping threshold.

    A path is classified *structural* when it consists exclusively of straight
    line segments and verified perfect circles (see :func:`_is_perfect_circle`).
    The document is structural when the fraction of structural paths strictly
    exceeds ``structural_ratio``.

    Example:
        >>> from plt_optimizer.core.parser import PLTParser
        >>> parser = PLTParser()
        >>> doc = parser.parse_string("IN;PU0,0;PD100,0;PD100,50;SP;")
        >>> profiler = Profiler()
        >>> result = profiler.profile(doc)
        >>> print(result.baseline_extent)
        102.345
    """

    def __init__(self, structural_ratio: float = DEFAULT_STRUCTURAL_RATIO) -> None:
        """Initialize the Profiler.

        Args:
            structural_ratio: Document-level gate in (0, 1]. A document is
                classified structural when the fraction of structural paths
                strictly exceeds this value. Defaults to
                :data:`DEFAULT_STRUCTURAL_RATIO` (0.85).
        """
        self._structural_ratio = structural_ratio
        self._logger = get_text_logger()

    def profile(self, document: StrokePathsProtocol) -> ProfileResult:
        """Analyze a PLT document and calculate baseline extent.

        Args:
            document: A sequence of stroke paths to analyze (typically PLTDocument).

        Returns:
            A ProfileResult containing the baseline_extent and statistics.
            For structural documents that contain no straight cutting lines
            (pure-circle files), the baseline statistics are all zero instead
            of raising: structural chunking ignores the baseline entirely.

        Raises:
            ProfilerError: If no straight cutting strokes are found and the
                document is not structural.
        """
        self._logger.info("Starting baseline extent profiling")

        # Calculate polyline density & structural composition
        valid_paths = [p for p in document.stroke_paths if p.segments]
        total_paths = len(valid_paths)
        total_segments = sum(len(p.segments) for p in valid_paths)

        avg_segments_per_path = total_segments / total_paths if total_paths > 0 else 0

        # Structural composition: a path is structural when it contains only
        # straight lines and verified perfect circles (drill holes). The high
        # default ratio gate (85%) keeps mixed files (text + holes) on the
        # conservative text path, where holes ride along in chronological
        # blocks instead of being fractured.
        if total_paths > 0:
            structural_path_count = sum(1 for p in valid_paths if self._is_structural_path(p))
            structural_ratio = structural_path_count / total_paths
            is_structural = structural_ratio > self._structural_ratio
        else:
            structural_path_count = 0
            structural_ratio = 0.0
            is_structural = False

        self._logger.debug(
            f"Polyline density analysis: {total_paths} paths, "
            f"{total_segments} total segments, "
            f"avg {avg_segments_per_path:.1f} segments/path, "
            f"structural={structural_path_count}/{total_paths} ({structural_ratio:.1%})"
        )

        # Collect all extents from cutting line segments (holes excluded).
        extents = self._calculate_all_extents(document)

        if not extents:
            has_cutting_arcs = any(
                segment.is_cutting
                for path in valid_paths
                for segment in path.segments
                if isinstance(segment, ArcSegment)
            )
            if is_structural and has_cutting_arcs:
                # Pure-circle structural file: structural chunking bypasses
                # baseline-based grouping, so a zero baseline is safe.
                self._logger.info(
                    f"Profiling complete: is_structural=True "
                    f"({structural_ratio:.1%} structural), no straight cutting "
                    f"strokes found; baseline_extent=0.0 (ignored for structural files)"
                )
                return ProfileResult(
                    baseline_extent=0.0,
                    median_dx=0.0,
                    median_dy=0.0,
                    total_strokes=0,
                    p95_index=0,
                    is_structural=True,
                )
            raise ProfilerError(
                "No cutting strokes found in document. Cannot calculate baseline extent."
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
            f"{structural_ratio:.1%} structural, gate {self._structural_ratio:.1%}), "
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
                    # Arc segments (drill holes) are excluded from the baseline
                    # by design: hole size must never skew the text-grouping
                    # threshold derived from this statistic.
                    continue

                dx = abs(end.x - start.x)
                dy = abs(end.y - start.y)

                if dx > 0 or dy > 0:  # Ignore zero-length segments
                    extents.append(Extent(dx=dx, dy=dy))

        return extents

    def _is_structural_path(self, path: StrokePath) -> bool:
        """Determine if a single path is structural (straight lines + perfect circles).

        Compositional rule: a path is structural when *every* segment is either
        a straight line (any length, including zero-length plunge points that
        open drill-hole paths) or part of a consecutive arc run verified as a
        perfect circle by :func:`_is_perfect_circle` (e.g. an EngraveLab hole
        emitted as four chained 90-degree arcs sharing one center and radius).

        This deliberately rejects text: EngraveLab renders glyph curves as many
        tiny arcs whose centers/radii change every segment and whose sweeps
        never total a full revolution, while generated text is multi-segment
        polyline runs. A path mixing a verified circle with arc fragments also
        fails, because at least one run is not a circle.

        Args:
            path: The stroke path to classify.

        Returns:
            True if the path consists only of straight lines and perfect circles.
        """
        if not path.segments:
            return False

        for run in _arc_runs(path.segments):
            if not _is_perfect_circle(run):
                return False

        return True


@dataclass(frozen=True)
class ProfileResult:
    """Results from baseline extent profiling analysis.

    Attributes:
        baseline_extent: The 95th percentile of max bounding box dimension.
            Used as the threshold multiplier base in chunking.
        median_dx: Median width across all strokes.
        median_dy: Median height across all strokes.
        total_strokes: Number of cutting stroke segments analyzed (straight
            lines only; circles are excluded from baseline statistics).
        p95_index: Index into sorted dimensions that corresponds to 95th percentile.
        is_structural: True if the file contains structural features (drill holes,
            score lines) instead of design geometry. A file is classified as
            structural when more than ``structural_ratio`` (default 85%) of its
            paths consist exclusively of:
            - Straight line segments (any length, including zero-length plunges)
            - Verified perfect circles: consecutive arc runs that chain end-to-start,
              share one center and radius, and total ~360° sweep (e.g. EngraveLab
              4x 90-degree drill holes, 2x 180-degree pairs, or single CI circles)
    """

    baseline_extent: float
    median_dx: float
    median_dy: float
    total_strokes: int
    p95_index: int
    is_structural: bool


class StrokePathsProtocol(Protocol):
    """Protocol for objects that contain stroke path data.

    This protocol allows the Profiler to work with any object that provides
    an iterable `stroke_paths` attribute, such as PLTDocument.
    """

    @property
    def stroke_paths(self) -> Sequence[StrokePath]:
        """Return the sequence of stroke paths."""
        ...
