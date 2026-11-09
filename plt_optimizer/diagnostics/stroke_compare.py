"""Strict stroke-level comparison of two PLT toolpaths (matplotlib-free).

The relief engraving equivalence tests measure MATERIAL REMOVED (stroke
envelopes dilated by the tool radius) -- a loose geometric contract that
cannot see a missing raster row, a chain that lost its connectors, or an
outline drawn as 40 line segments where the reference cuts one arc. This
module compares two PLT files AS STROKES:

1. parse each file with the core :class:`~plt_optimizer.core.parser.PLTParser`
   and split every pen-down run into a stroke (``PU`` gaps, pen changes and
   rapid travel split runs; arcs are flattened to chords fine enough for the
   sampling step),
2. sample each stroke at a fixed arc-length step,
3. index the target's sample points on a cell grid and measure, for every
   stroke of each side, the mean/max distance from its samples to the
   nearest sample of the other file (bidirectional chamfer),
4. classify: a reference stroke whose mean distance exceeds the tolerance is
   MISSING (we do not reproduce it); an our-side stroke whose mean distance
   exceeds the tolerance is EXTRA (the reference does not have it).

Matching is pure geometry -- stroke order, chaining and pen routing are
irrelevant (our TSP routes toolpaths differently than EngraveLab's sweep
order). Coordinates are compared as parsed (device/plotter frame, inches):
the plate transform maps label geometry into the same frame the reference
files use, so no mirroring is applied here (see ``tests/relief_coverage.py``
for the established frame convention).

This module is pure stdlib + the core parser (no shapely, no matplotlib), so
it is import-safe on the Windows 7 runtime; the plotting wrapper lives in
``scripts/compare_relief_strokes.py`` (dev-only).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from plt_optimizer.core.models import ArcSegment, StrokePath
from plt_optimizer.core.parser import PLTParser
from plt_optimizer.core.units import units_to_inches

Point = Tuple[float, float]

#: One plotter unit in inches (1016 units/inch, root AGENTS.md §4) -- the
#: default match tolerance: a stroke reproduces the reference when its
#: samples lie within a plotter unit of a reference sample.
PLOTTER_UNIT = 1.0 / 1016.0

#: Default sample step (inches) along each stroke.
DEFAULT_SAMPLE = 5e-4

#: Endpoint match tolerance (inches) when splitting pen-down runs (parsed
#: coordinates are 3-decimal plotter units, so touching endpoints agree to
#: well under 1e-5 in -- same rule as ``stroke_geometry``).
_RUN_TOL = 1e-6


@dataclass(frozen=True)
class SampledStroke:
    """One pen-down run, sampled at a fixed step (inches, parsed frame).

    Attributes:
        index: Position of this stroke in its file (parse order).
        points: The sample points in cutting order (always >= 2).
        length: The (approximate) cutting length in inches.
        bounds: ``(min_x, min_y, max_x, max_y)`` of the samples.
        has_arc: True when the run contained at least one arc segment.
    """

    index: int
    points: Tuple[Point, ...]
    length: float
    bounds: Tuple[float, float, float, float]
    has_arc: bool

    @property
    def center(self) -> Point:
        """Center of the stroke's bounding box."""
        minx, miny, maxx, maxy = self.bounds
        return ((minx + maxx) / 2.0, (miny + maxy) / 2.0)


def _arc_samples(arc: ArcSegment, step: float) -> List[Point]:
    """Flatten an arc to points with (near-)unit spacing (inches)."""
    x0, y0 = units_to_inches(arc.start.x), units_to_inches(arc.start.y)
    cx, cy = units_to_inches(arc.center.x), units_to_inches(arc.center.y)
    radius = math.hypot(x0 - cx, y0 - cy)
    sweep = math.radians(arc.sweep_angle)
    length = abs(sweep) * radius
    count = max(2, int(math.ceil(length / max(step, _RUN_TOL))))
    a0 = math.atan2(y0 - cy, x0 - cx)
    pts: List[Point] = []
    for i in range(count + 1):
        angle = a0 + sweep * (i / count)
        pts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return pts


def _run_points(stroke: StrokePath, step: float) -> List[List[Point]]:
    """Split one parsed stroke path into pen-down runs of points (inches)."""
    runs: List[List[Point]] = []
    current: List[Point] = []
    if stroke.pen_up_position is not None:
        current = [
            (units_to_inches(stroke.pen_up_position.x), units_to_inches(stroke.pen_up_position.y))
        ]

    def _flush() -> None:
        nonlocal current
        if len(current) >= 2:
            runs.append(current)
        current = []

    for segment in stroke.segments:
        pts: List[Point] = []
        if isinstance(segment, ArcSegment):
            pts = _arc_samples(segment, step)
        else:
            pts = [
                (units_to_inches(segment.start.x), units_to_inches(segment.start.y)),
                (units_to_inches(segment.end.x), units_to_inches(segment.end.y)),
            ]
        if not segment.is_cutting:
            _flush()
            current = [pts[-1]]
            continue
        if current and math.dist(current[-1], pts[0]) <= _RUN_TOL:
            current.extend(pts[1:])
        else:
            _flush()
            current = pts
    _flush()
    return runs


def _resample(points: Sequence[Point], step: float) -> Tuple[Point, ...]:
    """Resample a polyline at fixed arc-length spacing (last point kept)."""
    out: List[Point] = [points[0]]
    carry = 0.0  # Distance already walked on the current segment.
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        seg_len = math.hypot(x1 - x0, y1 - y0)
        if seg_len <= _RUN_TOL:
            continue
        remaining = step - carry
        pos = 0.0
        while pos + remaining <= seg_len:
            pos += remaining
            ratio = pos / seg_len
            out.append((x0 + (x1 - x0) * ratio, y0 + (y1 - y0) * ratio))
            remaining = step
        carry = seg_len - pos
    if math.dist(out[-1], points[-1]) > _RUN_TOL:
        out.append(points[-1])
    return tuple(out)


def _stroke_length(points: Sequence[Point]) -> float:
    """Total polyline length (inches)."""
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def parse_strokes(path: Path, sample: float = DEFAULT_SAMPLE) -> List[SampledStroke]:
    """Parse a PLT file into sampled pen-down strokes (inches).

    Args:
        path: The PLT file.
        sample: Target spacing (inches) between sample points along a
            stroke (and the arc-flattening chord scale).

    Returns:
        Strokes in parse order; zero-length and single-point runs dropped.
    """
    doc = PLTParser().parse_file(path)
    strokes: List[SampledStroke] = []
    for stroke in doc.stroke_paths:
        for run in _run_points(stroke, sample):
            pts = _resample(run, sample)
            if len(pts) < 2:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            strokes.append(
                SampledStroke(
                    index=len(strokes),
                    points=pts,
                    length=_stroke_length(pts),
                    bounds=(min(xs), min(ys), max(xs), max(ys)),
                    has_arc=any(isinstance(s, ArcSegment) for s in stroke.segments),
                )
            )
    return strokes


class _PointGrid:
    """Uniform cell grid for nearest-point distance queries (2D)."""

    def __init__(self, strokes: Sequence[SampledStroke], cell: float) -> None:
        """Bucket every sample point into cells of ``cell`` inches."""
        self._cell = max(cell, _RUN_TOL)
        self._buckets: Dict[Tuple[int, int], List[Point]] = {}
        for stroke in strokes:
            for point in stroke.points:
                key = (
                    int(math.floor(point[0] / self._cell)),
                    int(math.floor(point[1] / self._cell)),
                )
                self._buckets.setdefault(key, []).append(point)

    def nearest_distance(self, x: float, y: float) -> float:
        """Distance from ``(x, y)`` to the nearest indexed point (inf if empty)."""
        if not self._buckets:
            return math.inf
        cx = int(math.floor(x / self._cell))
        cy = int(math.floor(y / self._cell))
        best = math.inf
        ring = 0
        while ring <= 64:
            if ring == 0:
                cells = [(cx, cy)]
            else:
                cells = []
                for dx in range(-ring, ring + 1):
                    for dy in range(-ring, ring + 1):
                        if max(abs(dx), abs(dy)) == ring:
                            cells.append((cx + dx, cy + dy))
            for key in cells:
                for point in self._buckets.get(key, ()):
                    dist = math.dist((x, y), point)
                    if dist < best:
                        best = dist
            if best <= ring * self._cell:
                break
            ring += 1
        return best


def _stroke_error(stroke: SampledStroke, grid: _PointGrid) -> Tuple[float, float]:
    """Mean/max distance from a stroke's samples to the nearest indexed point."""
    dists = [grid.nearest_distance(x, y) for x, y in stroke.points]
    if not dists:
        return (math.inf, math.inf)
    return (sum(dists) / len(dists), max(dists))


@dataclass(frozen=True)
class StrokeVerdict:
    """One stroke's comparison outcome.

    Attributes:
        index: Stroke index in its file.
        length: Cutting length (inches).
        bounds: ``(min_x, min_y, max_x, max_y)``.
        mean_distance: Mean sample distance to the other file (inches).
        max_distance: Worst sample distance (inches).
        status: ``"matched"``, ``"missing"`` (reference-only) or
            ``"extra"`` (ours-only).
        has_arc: True when the stroke contained arc geometry.
    """

    index: int
    length: float
    bounds: Tuple[float, float, float, float]
    mean_distance: float
    max_distance: float
    status: str
    has_arc: bool

    def to_dict(self) -> Dict[str, object]:
        """JSON-safe dict (inf -> null)."""

        def _num(value: float) -> Optional[float]:
            return None if math.isinf(value) else round(value, 6)

        return {
            "index": self.index,
            "length": round(self.length, 6),
            "bounds": [round(v, 6) for v in self.bounds],
            "mean_distance": _num(self.mean_distance),
            "max_distance": _num(self.max_distance),
            "status": self.status,
            "has_arc": self.has_arc,
        }


@dataclass(frozen=True)
class ComparisonReport:
    """Bidirectional stroke comparison of one PLT pair.

    Attributes:
        ours_path: Path of the generated file (for the report header).
        ref_path: Path of the reference file.
        tol: Match tolerance in inches.
        sample: Sample step in inches.
        ours: Per-our-stroke verdicts (extras are status ``extra``).
        ref: Per-reference-stroke verdicts (missing are status ``missing``).
    """

    ours_path: str
    ref_path: str
    tol: float
    sample: float
    ours: Tuple[StrokeVerdict, ...]
    ref: Tuple[StrokeVerdict, ...]

    @property
    def missing(self) -> List[StrokeVerdict]:
        """Reference strokes we do not reproduce."""
        return [v for v in self.ref if v.status == "missing"]

    @property
    def extra(self) -> List[StrokeVerdict]:
        """Our strokes the reference does not have."""
        return [v for v in self.ours if v.status == "extra"]

    @property
    def matched_ref(self) -> List[StrokeVerdict]:
        """Reference strokes reproduced within tolerance."""
        return [v for v in self.ref if v.status == "matched"]

    @property
    def matched_error(self) -> Tuple[float, float]:
        """(mean, max) matched-stroke mean-distance over matched ref strokes."""
        values = [v.mean_distance for v in self.matched_ref]
        if not values:
            return (0.0, 0.0)
        return (sum(values) / len(values), max(values))

    @property
    def exit_code(self) -> int:
        """0 when nothing is missing/extra within tolerance, 1 otherwise."""
        return 0 if not self.missing and not self.extra else 1

    def to_dict(self) -> Dict[str, object]:
        """Full JSON-safe report."""
        mean, maximum = self.matched_error
        return {
            "ours": self.ours_path,
            "reference": self.ref_path,
            "tol_inches": self.tol,
            "sample_inches": self.sample,
            "counts": {
                "ours": len(self.ours),
                "reference": len(self.ref),
                "matched": len(self.matched_ref),
                "missing": len(self.missing),
                "extra": len(self.extra),
            },
            "matched_error_inches": {"mean": round(mean, 6), "max": round(maximum, 6)},
            "ours_strokes": [v.to_dict() for v in self.ours],
            "reference_strokes": [v.to_dict() for v in self.ref],
        }

    def format_report(self, max_rows: int = 40) -> str:
        """Human-readable summary + the missing/extra tables (worst first)."""
        mean, maximum = self.matched_error
        lines = [
            f"stroke comparison: {self.ours_path} vs {self.ref_path}",
            f"tol={self.tol:.6f} in  sample={self.sample:.6f} in",
            f"reference strokes: {len(self.ref)}  ours: {len(self.ours)}",
            f"matched: {len(self.matched_ref)}  missing: {len(self.missing)}  extra: {len(self.extra)}",
            f"matched mean-distance: mean {mean:.6f} in  max {maximum:.6f} in",
        ]
        if self.missing:
            lines.append("")
            lines.append(
                f"MISSING reference strokes (not reproduced by ours), {len(self.missing)}:"
            )
            lines.append(f"{'idx':>6} {'length':>9} {'mean_d':>9} {'max_d':>9}  bounds (inches)")
            for verdict in sorted(self.missing, key=lambda v: -v.mean_distance)[:max_rows]:
                bounds = " ".join(f"{v:.3f}" for v in verdict.bounds)
                lines.append(
                    f"{verdict.index:>6} {verdict.length:>9.4f} "
                    f"{verdict.mean_distance:>9.5f} {verdict.max_distance:>9.5f}  {bounds}"
                )
        if self.extra:
            lines.append("")
            lines.append(f"EXTRA our strokes (not in the reference), {len(self.extra)}:")
            lines.append(f"{'idx':>6} {'length':>9} {'mean_d':>9} {'max_d':>9}  bounds (inches)")
            for verdict in sorted(self.extra, key=lambda v: -v.mean_distance)[:max_rows]:
                bounds = " ".join(f"{v:.3f}" for v in verdict.bounds)
                lines.append(
                    f"{verdict.index:>6} {verdict.length:>9.4f} "
                    f"{verdict.mean_distance:>9.5f} {verdict.max_distance:>9.5f}  {bounds}"
                )
        if not self.missing and not self.extra:
            lines.append("PASS: every stroke reproduced within tolerance, no extras.")
        return "\n".join(lines)


def compare_strokes(
    ours: Sequence[SampledStroke],
    ref: Sequence[SampledStroke],
    tol: float = PLOTTER_UNIT,
    sample: float = DEFAULT_SAMPLE,
    ours_path: str = "ours",
    ref_path: str = "reference",
) -> ComparisonReport:
    """Bidirectional stroke chamfer comparison.

    Args:
        ours: Strokes of the generated file (:func:`parse_strokes`).
        ref: Strokes of the reference file.
        tol: A stroke matches when its sample points lie within ``tol``
            (mean distance) of the other file.
        sample: Sample step (used for the grid cell size).
        ours_path: Names for the report.
        ref_path: See ``ours_path``.

    Returns:
        The :class:`ComparisonReport`.
    """
    ours_grid = _PointGrid(ours, cell=max(tol, sample))
    ref_grid = _PointGrid(ref, cell=max(tol, sample))
    ref_verdicts: List[StrokeVerdict] = []
    for stroke in ref:
        mean_d, max_d = _stroke_error(stroke, ours_grid)
        ref_verdicts.append(
            StrokeVerdict(
                index=stroke.index,
                length=stroke.length,
                bounds=stroke.bounds,
                mean_distance=mean_d,
                max_distance=max_d,
                status="matched" if mean_d <= tol else "missing",
                has_arc=stroke.has_arc,
            )
        )
    our_verdicts: List[StrokeVerdict] = []
    for stroke in ours:
        mean_d, max_d = _stroke_error(stroke, ref_grid)
        our_verdicts.append(
            StrokeVerdict(
                index=stroke.index,
                length=stroke.length,
                bounds=stroke.bounds,
                mean_distance=mean_d,
                max_distance=max_d,
                status="matched" if mean_d <= tol else "extra",
                has_arc=stroke.has_arc,
            )
        )
    return ComparisonReport(
        ours_path=ours_path,
        ref_path=ref_path,
        tol=tol,
        sample=sample,
        ours=tuple(our_verdicts),
        ref=tuple(ref_verdicts),
    )


def compare_files(
    ours_path: Path,
    ref_path: Path,
    tol: float = PLOTTER_UNIT,
    sample: float = DEFAULT_SAMPLE,
) -> ComparisonReport:
    """Parse both PLT files and run :func:`compare_strokes`."""
    ours = parse_strokes(ours_path, sample=sample)
    ref = parse_strokes(ref_path, sample=sample)
    return compare_strokes(
        ours, ref, tol=tol, sample=sample, ours_path=str(ours_path), ref_path=str(ref_path)
    )


__all__ = [
    "ComparisonReport",
    "PLOTTER_UNIT",
    "SampledStroke",
    "StrokeVerdict",
    "compare_files",
    "compare_strokes",
    "parse_strokes",
]
