#!/usr/bin/env python3
"""Reverse-engineer PLT fonts from engraved, reference-framed ASCII sample sheets.

EngraveLab / Vision Pro cannot export their vector fonts, but they can
*engrave* them. The workflow (see ``How to extract fonts from EngraveLab or
VisionPro.md`` beside this script) is:

1. In EngraveLab/Vision Pro, use the **Text Compose** tool (never *Frame Text
   Compose*, which compresses to fit the plate) and type the characters of
   ``Fonts/ascii.txt`` in order, split across as many rows as the plate
   needs. **Every row must start and end with one chosen reference
   character** (e.g. ``E``), which acts as structural framing only.
2. Engrave the sheet to a PLT file.
3. Drop the PLT into ``Fonts/PLT/`` named
   ``<font name>_<declared height in>_<reference char>.plt`` (e.g.
   ``dino_0.5_E.plt``).
4. Run this script::

       uv run python Fonts/extract_plt_fonts.py

Each sheet is parsed with the core :class:`~plt_optimizer.core.parser.PLTParser`
and verified structurally before any glyph data is trusted:

* **Row detection** - stroke paths are grouped into rows by single-linkage
  merging of their Y extents, and the split is searched until the layout is
  self-consistent (see :func:`find_layout`).
* **Count check** - the characters inside the framing must match
  ``Fonts/ascii.txt`` exactly (``sum(n_row - 2) == len(characters)``).
* **Framing check** - the internal copy of the reference character (the one
  that ``ascii.txt`` assigns a real character slot) must coincide with every
  row-framing copy to within :data:`REFERENCE_MATCH_TOLERANCE` units once
  both are translated to a common origin. This proves the row split and the
  character alignment are correct, not merely plausible.

Each glyph is then normalized with one global similarity transform derived
from the *measured* reference character:

``X_norm = S * (X_raw - X_min_glyph)`` and ``Y_norm = S * (Y_baseline_row - Y_raw)``
with ``S = UNITS_PER_INCH / H_ref_raw``

so stored values stay in plotter units (1000 units = 1 inch), the reference
character stands exactly 1000 units tall, every glyph sits on ``y = 0`` with
its left edge at ``x = 0``, and Y grows upward (descending characters get
negative ``y``). The raw sheets are engraved in the device convention (+Y
down), so this transform mirrors Y and therefore negates every ``AA`` sweep
angle. ``H_ref_raw`` is the median measured height across all framing copies;
the file-name height is *never* used to correct it (see
:func:`check_height_drift`).

The result is merged into ``Fonts/plt_fonts.json``::

    {
      "<Font Name>": {
        "file_path": "Fonts/PLT/dino_0.5_E.plt",
        "reference_char": "E",
        "declared_height_in": 0.5,
        "reference_char_height_in": 0.508017,
        "normalized_ref_height": 1.0,
        "characters": {
          "E": {
            "bounding_box": {"min_x": 0.0, "max_x": ..., "min_y": 0.0, "max_y": 1000.0},
            "left_envelope": [[x, y], ...],
            "right_envelope": [[x, y], ...],
            "glyph": "PU0.0000,0.0000;PD...;"
          }, ...
        }
      }, ...
    }

``left_envelope`` / ``right_envelope`` are the profile of the glyph sampled on
a uniform vertical grid (:data:`ENVELOPE_SAMPLES` rows), which lets the
typesetter kern tightly by measuring real air gaps instead of fixed advance
widths. Font keys are the file-name font part with underscores turned into
spaces and title-cased (``heavy_engraving_0.5_E.plt`` -> ``"Heavy
Engraving"``); use ``--font-name`` to override for a single file. Existing
fonts in the JSON are preserved unless ``--rebuild`` is given.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow running as a plain script
    sys.path.insert(0, str(REPO_ROOT))

from plt_optimizer.core.models import (  # noqa: E402
    ArcSegment,
    PLTDocument,
    Segment,
    StrokePath,
)
from plt_optimizer.core.parser import ParseError, PLTParser  # noqa: E402

logger = logging.getLogger("extract_plt_fonts")

FONTS_DIR = REPO_ROOT / "Fonts"
DEFAULT_INPUT_DIR = FONTS_DIR / "PLT"
DEFAULT_OUTPUT = FONTS_DIR / "plt_fonts.json"
DEFAULT_ASCII_FILE = FONTS_DIR / "ascii.txt"

# Plotter units per inch (the HPGL unit the sample sheets are engraved in).
UNITS_PER_INCH = 1000.0

# Vertical samples per left/right profile envelope; overridable with
# --envelope-samples.
ENVELOPE_SAMPLES = 30

# Declared-vs-measured height drift above which a WARNING is logged. The
# measured height is always stored unadjusted; this only flags likely
# EngraveLab setup mistakes (wrong reference char, mis-sized framing).
DRIFT_WARN_THRESHOLD = 0.05

# Maximum coordinate difference, in raw plotter units, between the internal
# reference glyph and a row-framing copy for the sheet to be accepted. The
# core parser rounds every :class:`~plt_optimizer.core.models.Coordinate` to
# 3 decimals, so two *identical* glyphs can differ by up to 0.001 units; the
# comparison therefore accepts this bound (plus a tiny float slack).
REFERENCE_MATCH_TOLERANCE = 1e-3

# Numerical slack (plotter units) for the geometric predicates (row/cluster
# merging, envelope sampling).
_EPS = 1e-9

# Bounding-box type: (x_min, y_min, x_max, y_max) in plotter units.
Bounds = Tuple[float, float, float, float]

# One JSON entry per character, and per font.
CharacterEntry = Dict[str, Any]
FontEntry = Dict[str, Any]


class FontExtractionError(Exception):
    """Raised when a PLT sample sheet cannot be extracted into a font."""


@dataclass(frozen=True)
class Interval:
    """A closed interval ``[x_min, x_max]`` on one axis.

    Attributes:
        x_min: Lower bound (plotter units).
        x_max: Upper bound (plotter units).
    """

    x_min: float
    x_max: float

    def gap_to(self, other: Interval) -> float:
        """Return the (possibly negative) gap from this interval's end to ``other``."""
        return other.x_min - self.x_max


@dataclass(frozen=True)
class Cluster:
    """A set of stroke paths belonging to one glyph.

    Attributes:
        paths: Member stroke paths in chronological (file) order.
        bounds: Union bounding box of the members, in plotter units.
    """

    paths: Tuple[StrokePath, ...]
    bounds: Bounds

    @property
    def center_x(self) -> float:
        """Return the X center of the cluster bounding box."""
        return (self.bounds[0] + self.bounds[2]) / 2.0


@dataclass(frozen=True)
class SheetRow:
    """One engraved row of a sample sheet.

    Attributes:
        clusters: The row's glyph clusters, sorted left-to-right. The first
            and last entries are the reference-character framing; everything
            between them is ``ascii.txt`` payload.
        baseline: The row's text baseline in raw plotter units. Engraved
            sheets use the device convention (+Y down), so this is the
            *maximum* Y of the framing glyph.
        reference_height: The measured height of the framing reference
            glyph, in raw plotter units.
    """

    clusters: Tuple[Cluster, ...]
    baseline: float
    reference_height: float

    @property
    def content(self) -> Tuple[Cluster, ...]:
        """Return the payload clusters (framing stripped)."""
        return self.clusters[1:-1]


@dataclass(frozen=True)
class GlyphTransform:
    """The normalization applied to one glyph's raw coordinates.

    ``X_norm = scale * (X_raw - origin_x)`` and
    ``Y_norm = scale * (baseline - Y_raw)``. Because Y is reflected, the
    transform has a negative determinant and every arc sweep angle must be
    negated to preserve the cut direction.

    Attributes:
        scale: Uniform scale factor ``S = UNITS_PER_INCH / H_ref_raw``.
        origin_x: The glyph's raw left edge, mapped to ``x = 0``.
        baseline: The row's raw baseline Y, mapped to ``y = 0``.
    """

    scale: float
    origin_x: float
    baseline: float

    def apply(self, x: float, y: float) -> Tuple[float, float]:
        """Map one raw point to normalized plotter units (+Y up).

        Args:
            x: Raw X coordinate.
            y: Raw Y coordinate.

        Returns:
            The ``(x, y)`` pair in the stored glyph frame.
        """
        return self.scale * (x - self.origin_x), self.scale * (self.baseline - y)

    def map_bounds(self, bounds: Bounds) -> Bounds:
        """Map a raw bounding box, accounting for the Y reflection.

        Args:
            bounds: ``(x_min, y_min, x_max, y_max)`` in raw plotter units.

        Returns:
            The mapped ``(x_min, y_min, x_max, y_max)``.
        """
        low_x, low_y = self.apply(bounds[0], bounds[3])
        high_x, high_y = self.apply(bounds[2], bounds[1])
        return (min(low_x, high_x), min(low_y, high_y), max(low_x, high_x), max(low_y, high_y))


@dataclass(frozen=True)
class FontExtraction:
    """The result of extracting one PLT sample sheet.

    Attributes:
        font_name: Font key used in ``plt_fonts.json``.
        entry: The font's JSON entry (metadata plus ``characters``).
        row_count: Number of engraved rows detected on the sheet.
        row_threshold: Y-gap row-splitting threshold actually used.
        cluster_threshold: X-gap clustering threshold actually used.
    """

    font_name: str
    entry: FontEntry
    row_count: int
    row_threshold: float
    cluster_threshold: float


def load_characters(ascii_file: Path) -> List[str]:
    """Load the ordered printable-ASCII character list.

    Every whitespace run in the file is a separator; the remaining
    characters, in file order, are the expected glyph sequence. The framing
    reference characters are *not* part of this file - they exist only in the
    engraved sheet and are stripped before alignment.

    Args:
        ascii_file: Path to ``Fonts/ascii.txt``.

    Returns:
        Ordered list of characters (94 for the shipped ``ascii.txt``).

    Raises:
        FontExtractionError: If the file is missing or contains no characters.
    """
    try:
        content = ascii_file.read_text(encoding="utf-8")
    except OSError as e:
        raise FontExtractionError(f"Failed to read character file {ascii_file}: {e}") from e
    characters = "".join(content.split())
    if not characters:
        raise FontExtractionError(f"Character file {ascii_file} contains no characters")
    return list(characters)


def parse_font_file_name(plt_path: Path) -> Tuple[str, float, str]:
    """Split an engraved sample file name into font name, height and reference.

    The convention is ``<font name>_<declared height in>_<ref char>.plt``
    (e.g. ``dino_0.5_E.plt``). The declared height is the text height typed
    into EngraveLab/Vision Pro; it is stored as metadata only - the actual
    normalization scale comes from the *measured* reference character. The
    font name may contain underscores, which become spaces.

    Args:
        plt_path: Path to the engraved sample PLT.

    Returns:
        Tuple of ``(font_name, declared_height_inches, reference_char)``.

    Raises:
        FontExtractionError: If the file name is malformed, carries a
            non-positive/non-finite height, or a reference character that is
            not a single character.
    """
    parts = plt_path.stem.rsplit("_", 2)
    usage = (
        f"{plt_path.name}: file name must be "
        f"'<font name>_<declared height in>_<ref char>.plt' "
        f"(e.g. 'dino_0.5_E.plt')"
    )
    if len(parts) != 3:
        raise FontExtractionError(f"{usage}; expected three '_' separated parts")
    name_part, height_part, reference_char = parts
    if not name_part:
        raise FontExtractionError(f"{usage}; font name is empty")
    try:
        declared_height = float(height_part)
    except ValueError as e:
        raise FontExtractionError(
            f"{usage}; {height_part!r} is not a declared text height in inches"
        ) from e
    if not math.isfinite(declared_height) or declared_height <= 0.0:
        raise FontExtractionError(f"{usage}; declared text height must be positive and finite")
    if len(reference_char) != 1:
        raise FontExtractionError(f"{usage}; reference character must be exactly one character")
    return name_part.replace("_", " ").title(), declared_height, reference_char


def arc_bounds(arc: ArcSegment) -> Bounds:
    """Return the bounding box of the arc's *swept* portion.

    EngraveLab approximates many near-straight glyph strokes as huge-radius
    best-fit arcs, so the arc's full circle dwarfs the actual cut. Measuring
    only the swept extent (the two endpoints plus any cardinal angle the arc
    passes through) keeps a glyph's footprint tight and lets adjacent
    characters separate cleanly. A full-revolution arc still yields its whole
    circle.

    Args:
        arc: The arc segment to measure.

    Returns:
        ``(x_min, y_min, x_max, y_max)`` in plotter units.
    """
    cx = arc.center.x
    cy = arc.center.y
    radius = arc.radius
    theta_start = math.atan2(arc.start.y - cy, arc.start.x - cx)
    theta_end = theta_start + math.radians(arc.sweep_angle)
    lo, hi = min(theta_start, theta_end), max(theta_start, theta_end)

    quarter = math.pi / 2.0
    angles = [theta_start, theta_end]
    k = math.floor(lo / quarter) - 1
    while True:
        cardinal = k * quarter
        if cardinal > hi:
            break
        if cardinal >= lo:
            angles.append(cardinal)
        k += 1

    xs = [cx + radius * math.cos(a) for a in angles]
    ys = [cy + radius * math.sin(a) for a in angles]
    return (min(xs), min(ys), max(xs), max(ys))


def segment_bounds(segment: Segment) -> Bounds:
    """Return the bounding box of one segment.

    Arcs use their swept extent (see :func:`arc_bounds`); lines use their two
    endpoints.

    Args:
        segment: The line or arc segment to measure.

    Returns:
        ``(x_min, y_min, x_max, y_max)`` in plotter units.
    """
    if isinstance(segment, ArcSegment):
        return arc_bounds(segment)
    return (
        min(segment.start.x, segment.end.x),
        min(segment.start.y, segment.end.y),
        max(segment.start.x, segment.end.x),
        max(segment.start.y, segment.end.y),
    )


def path_bounds(path: StrokePath) -> Bounds:
    """Return the bounding box of one stroke path.

    Args:
        path: The path to measure (must contain at least one segment).

    Returns:
        ``(x_min, y_min, x_max, y_max)`` in plotter units.

    Raises:
        ValueError: If the path has no segments.
    """
    if not path.segments:
        raise ValueError("Cannot bound a stroke path without segments")
    return union_bounds([segment_bounds(seg) for seg in path.segments])


def union_bounds(boxes: Sequence[Bounds]) -> Bounds:
    """Return the union bounding box of non-empty boxes.

    Args:
        boxes: Boxes to merge (must be non-empty).

    Returns:
        ``(x_min, y_min, x_max, y_max)`` covering every input box.

    Raises:
        ValueError: If ``boxes`` is empty.
    """
    if not boxes:
        raise ValueError("Cannot union an empty sequence of bounding boxes")
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def merge_intervals(intervals: Sequence[Interval], threshold: float) -> List[Interval]:
    """Merge intervals whose gap is at most ``threshold``.

    Single-linkage merge: sorted intervals are fused whenever the next one
    starts within ``threshold`` of the running right edge. The result is a
    disjoint, sorted list. Used for both row banding (Y) and glyph
    separation (X).

    Args:
        intervals: Arbitrary-order intervals.
        threshold: Maximum gap to merge, in plotter units.

    Returns:
        Disjoint sorted intervals covering the same spans.
    """
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: (iv.x_min, iv.x_max))
    merged: List[Interval] = [ordered[0]]
    for current in ordered[1:]:
        last = merged[-1]
        if last.gap_to(current) <= threshold:
            merged[-1] = Interval(last.x_min, max(last.x_max, current.x_max))
        else:
            merged.append(current)
    return merged


def candidate_thresholds(gaps: Sequence[float]) -> List[float]:
    """Return ascending cut candidates bracketing every observed gap.

    Merging with a threshold ``t`` cuts exactly the gaps strictly greater than
    ``t``. To be able to cut any subset of the distinct positive gaps, the
    candidates are the midpoints between consecutive distinct gaps (plus
    ``0.0``, which cuts every positive gap).

    Args:
        gaps: Observed gaps between merged intervals, any order.

    Returns:
        Ascending candidate thresholds, starting with ``0.0``.
    """
    ordered = sorted({g for g in gaps if g > 0.0})
    candidates = [0.0]
    for index, gap in enumerate(ordered):
        lower = ordered[index - 1] if index else 0.0
        candidates.append((lower + gap) / 2.0)
    return candidates


def cluster_paths(paths: Sequence[StrokePath], threshold: float) -> List[Cluster]:
    """Group stroke paths into glyph clusters along X.

    Paths are sorted by left edge and single-linkage merged: a path joins the
    current cluster while its ``x_min`` is within ``threshold`` of the running
    right edge. EngraveLab emits strokes in a scrambled order, so spatial
    clustering - not the chronological
    :class:`~plt_optimizer.core.chunker.Chunker` - is the reliable split here.

    Args:
        paths: Stroke paths of one row (order-independent).
        threshold: Maximum intra-glyph X gap, in plotter units.

    Returns:
        Clusters sorted left-to-right by bounding-box center X. Paths without
        segments are skipped.

    Raises:
        ValueError: If no path carries segments.
    """
    entries: List[Tuple[Interval, StrokePath]] = []
    for path in paths:
        if not path.segments:
            continue
        bounds = path_bounds(path)
        entries.append((Interval(bounds[0], bounds[2]), path))
    if not entries:
        raise ValueError("Cannot cluster an empty sequence of stroke paths")

    entries.sort(key=lambda item: (item[0].x_min, item[0].x_max))
    clusters: List[Cluster] = []
    group_paths: List[StrokePath] = [entries[0][1]]
    group_max = entries[0][0].x_max

    def _seal() -> None:
        boxes = [path_bounds(p) for p in group_paths]
        clusters.append(Cluster(paths=tuple(group_paths), bounds=union_bounds(boxes)))

    for interval, path in entries[1:]:
        if interval.x_min - group_max <= threshold:
            group_paths.append(path)
            group_max = max(group_max, interval.x_max)
        else:
            _seal()
            group_paths = [path]
            group_max = interval.x_max
    _seal()

    clusters.sort(key=lambda c: c.center_x)
    return clusters


def group_rows(paths: Sequence[StrokePath], threshold: float) -> List[List[StrokePath]]:
    """Group stroke paths into engraved rows along Y.

    Single-linkage merge of the paths' Y extents: the rows come back in
    reading order because the sheets are engraved in the device convention
    (+Y down), so the smallest Y is the topmost row.

    Args:
        paths: Stroke paths of one document (order-independent).
        threshold: Maximum intra-row Y gap, in plotter units.

    Returns:
        Rows of paths, top row first. Paths without segments are skipped.
    """
    entries: List[Tuple[Interval, StrokePath]] = []
    for path in paths:
        if not path.segments:
            continue
        bounds = path_bounds(path)
        entries.append((Interval(bounds[1], bounds[3]), path))
    if not entries:
        return []

    entries.sort(key=lambda item: (item[0].x_min, item[0].x_max))
    rows: List[List[StrokePath]] = []
    group_paths: List[StrokePath] = [entries[0][1]]
    group_max = entries[0][0].x_max
    for interval, path in entries[1:]:
        if interval.x_min - group_max <= threshold:
            group_paths.append(path)
            group_max = max(group_max, interval.x_max)
        else:
            rows.append(group_paths)
            group_paths = [path]
            group_max = interval.x_max
    rows.append(group_paths)
    return rows


def glyph_points(cluster: Cluster) -> List[Tuple[float, float]]:
    """Return every recorded point of a cluster, translated to its left-bottom.

    Used by the framing verification: two copies of the same glyph coincide
    once both are moved to a common origin, regardless of where they sit on
    the sheet. Stroke order is scrambled by EngraveLab, so the caller compares
    *sorted* point lists.

    Args:
        cluster: The glyph cluster to sample.

    Returns:
        Segment endpoints (arc endpoints are their swept endpoints) sorted
        lexicographically, relative to the cluster's bounding-box corner.
    """
    x_min, y_min = cluster.bounds[0], cluster.bounds[1]
    points: List[Tuple[float, float]] = []
    for path in cluster.paths:
        for segment in path.segments:
            points.append((segment.start.x - x_min, segment.start.y - y_min))
            points.append((segment.end.x - x_min, segment.end.y - y_min))
    return sorted(points)


def build_rows(
    paths: Sequence[StrokePath],
    row_threshold: float,
    cluster_threshold: float,
) -> List[SheetRow]:
    """Split paths into framed rows of glyph clusters.

    Args:
        paths: Stroke paths with at least one segment each.
        row_threshold: Y-gap threshold separating rows.
        cluster_threshold: X-gap threshold separating glyphs.

    Returns:
        Rows in reading order, each with at least three clusters and its
        baseline/reference height measured from the framing copies.

    Raises:
        ValueError: If a row yields fewer than three clusters (framing needs
            both ends plus payload) or clustering is impossible.
    """
    rows: List[SheetRow] = []
    for row_paths in group_rows(paths, row_threshold):
        clusters = cluster_paths(row_paths, cluster_threshold)
        if len(clusters) < 3:
            raise ValueError(
                f"a row yielded only {len(clusters)} glyph groups; every row needs a "
                f"reference character at both ends plus payload"
            )
        framing = union_bounds([clusters[0].bounds, clusters[-1].bounds])
        rows.append(
            SheetRow(
                clusters=tuple(clusters),
                baseline=framing[3],
                reference_height=framing[3] - framing[1],
            )
        )
    return rows


def verify_layout(rows: Sequence[SheetRow], characters: Sequence[str], reference_char: str) -> None:
    """Verify that a candidate layout is the sheet's true structure.

    Two independent checks run, in order:

    1. **Count** - ``sum(len(row.clusters) - 2)`` must equal the number of
       characters in ``ascii.txt``.
    2. **Framing geometry** - the internal copy of ``reference_char`` (the one
       occupying a real character slot) must match every framing copy to
       within :data:`REFERENCE_MATCH_TOLERANCE` units at a common origin.

    Args:
        rows: Candidate rows in reading order.
        characters: Expected characters, in order.
        reference_char: The reference character parsed from the file name.

    Raises:
        FontExtractionError: If either check fails; the message names the
            offending count or row so the operator can fix the sheet.
    """
    found = sum(len(row.clusters) - 2 for row in rows)
    if found != len(characters):
        direction = "missing" if found < len(characters) else "extra"
        raise FontExtractionError(
            f"Sheet carries {found} payload glyphs but the character list has "
            f"{len(characters)} ({abs(len(characters) - found)} {direction}): "
            f"{[len(row.clusters) - 2 for row in rows]} payload glyphs across "
            f"{len(rows)} rows. Check that every row starts and ends with the "
            f"reference character and that the rows follow the character file order."
        )

    content = [cluster for row in rows for cluster in row.content]
    reference_index = characters.index(reference_char)
    internal = glyph_points(content[reference_index])
    for row_index, row in enumerate(rows):
        for side, framing in (("first", row.clusters[0]), ("last", row.clusters[-1])):
            points = glyph_points(framing)
            if len(points) != len(internal):
                raise FontExtractionError(
                    f"Row {row_index + 1} {side} framing character {reference_char!r} has "
                    f"{len(points)} points but the internal copy has {len(internal)}; "
                    f"the row split does not match the engraved layout."
                )
            deviation = max(
                (max(abs(a[0] - b[0]), abs(a[1] - b[1])) for a, b in zip(internal, points)),
                default=0.0,
            )
            # The float slack absorbs representation noise at the tolerance
            # boundary (identical glyphs measure 0.001 +/- 1e-9 here); any
            # real misalignment is orders of magnitude larger.
            if deviation > REFERENCE_MATCH_TOLERANCE + 1e-6:
                raise FontExtractionError(
                    f"Row {row_index + 1} {side} framing character {reference_char!r} differs "
                    f"from the internal copy by {deviation:.6f} units "
                    f"(tolerance {REFERENCE_MATCH_TOLERANCE:g}); the row boundaries or the "
                    f"character alignment are wrong."
                )


def find_layout(
    paths: Sequence[StrokePath],
    characters: Sequence[str],
    reference_char: str,
    row_threshold: Optional[float] = None,
    cluster_threshold: Optional[float] = None,
) -> Tuple[List[SheetRow], float, float]:
    """Search for the row/X split that reproduces the character list exactly.

    The sheet's row pitch and character pitch are not recorded anywhere, so
    both thresholds are recovered from the data. Candidate thresholds are the
    midpoints between consecutive observed gaps (:func:`candidate_thresholds`),
    which guarantees every possible grouping is tried. Rows are searched
    finest-first (smallest row threshold, i.e. most rows) and, within a row
    split, glyph thresholds ascending (least merging); the first candidate that
    passes :func:`verify_layout` wins. Requiring the framing geometry to match
    makes a wrong split self-reporting rather than silently mislabeling glyphs.

    Args:
        paths: Stroke paths with at least one segment each.
        characters: Expected characters, in order.
        reference_char: Reference character from the file name.
        row_threshold: Force a specific Y-gap threshold instead of searching.
        cluster_threshold: Force a specific X-gap threshold instead of searching.

    Returns:
        ``(rows, row_threshold, cluster_threshold)`` of the accepted layout.

    Raises:
        FontExtractionError: If no candidate layout reproduces the character
            list and the framing geometry.
    """
    if not paths:
        raise FontExtractionError("Document contains no stroke geometry")
    if reference_char not in characters:
        raise FontExtractionError(
            f"Reference character {reference_char!r} is not part of the character file; "
            f"framing verification requires it to be engraved as payload too."
        )

    y_intervals = [Interval(path_bounds(p)[1], path_bounds(p)[3]) for p in paths]
    y_bands = merge_intervals(y_intervals, 0.0)
    row_candidates: Sequence[float] = (
        [row_threshold]
        if row_threshold is not None
        else candidate_thresholds([b.gap_to(n) for b, n in zip(y_bands, y_bands[1:])])
    )

    last_error: Optional[FontExtractionError] = None
    for row_candidate in row_candidates:
        grouped = group_rows(paths, row_candidate)
        if any(len(row_paths) < 3 for row_paths in grouped):
            continue
        # X-gap candidates are per-row: rows overlap in X, so a global X band
        # merge would hide the intra-row gaps that matter here.
        x_gaps: List[float] = []
        for row_paths in grouped:
            row_bands = merge_intervals(
                [Interval(path_bounds(p)[0], path_bounds(p)[2]) for p in row_paths], 0.0
            )
            x_gaps.extend(b.gap_to(n) for b, n in zip(row_bands, row_bands[1:]))
        x_candidates: Sequence[float] = (
            [cluster_threshold] if cluster_threshold is not None else candidate_thresholds(x_gaps)
        )
        for x_candidate in x_candidates:
            try:
                rows = build_rows(paths, row_candidate, x_candidate)
                verify_layout(rows, characters, reference_char)
            except (ValueError, FontExtractionError) as e:
                last_error = (
                    e if isinstance(e, FontExtractionError) else FontExtractionError(str(e))
                )
                continue
            return rows, row_candidate, x_candidate

    detail = f" Last failure: {last_error}" if last_error else ""
    raise FontExtractionError(
        f"Could not split the sheet into {len(characters)} payload glyphs framed by "
        f"{reference_char!r} on every row.{detail} Pass --row-threshold / "
        f"--cluster-threshold (plotter units, 1000 = 1 inch) if the sheet uses an "
        f"unusual spacing."
    )


def median(values: Sequence[float]) -> float:
    """Return the median of ``values`` (mean of the middle pair when even).

    Args:
        values: Non-empty sequence of finite numbers.

    Returns:
        The median value.

    Raises:
        ValueError: If ``values`` is empty.
    """
    if not values:
        raise ValueError("Cannot take the median of an empty sequence")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def check_height_drift(
    font_name: str,
    plt_path: Path,
    declared_height: float,
    reference_char: str,
    reference_height: float,
) -> float:
    """Warn when the declared and measured heights disagree.

    The measured height is ground truth and is stored unadjusted - the raw
    geometry legitimately differs from the typed text height because of
    stroke-width compensation and CAD arc fitting. A large drift, however,
    usually means the file name's reference character is not the one actually
    framing the rows, or the framing was engraved at another size, so the
    operator is told about it. Execution is never halted: the vector math
    stays internally consistent either way.

    Args:
        font_name: Font key being extracted.
        plt_path: Source sheet (for the message).
        declared_height: Height parsed from the file name, in inches.
        reference_char: Reference character used for the measurement.
        reference_height: Measured reference height, in inches.

    Returns:
        The relative drift between declared and measured heights.
    """
    drift = abs(reference_height - declared_height) / declared_height
    if drift <= DRIFT_WARN_THRESHOLD:
        return drift
    logger.warning(
        "Font %r (%s):\n"
        "  Declared Height:  %.3f in\n"
        "  Computed Height:  %.3f in (Reference Char: %r)\n"
        "  Drift Detected:   %.1f%% (Exceeds %.1f%% threshold)\n"
        "  Action: Stored measured height without corrective adjustment.\n"
        "  Check: Verify that the correct reference character was specified in the file "
        "name and that framing characters in the PLT file are sized correctly.",
        font_name,
        plt_path.name,
        declared_height,
        reference_height,
        reference_char,
        drift * 100.0,
        DRIFT_WARN_THRESHOLD * 100.0,
    )
    return drift


def _format_number(value: float) -> str:
    """Format one normalized coordinate with 4-decimal precision.

    Args:
        value: Numeric value in normalized plotter units.

    Returns:
        Fixed-point string, e.g. ``"1234.5678"``. Negative zero is normalized
        to ``"0.0000"`` so the JSON stays free of ``-0.0000`` noise.
    """
    text = f"{value:.4f}"
    return "0.0000" if text == "-0.0000" else text


def _round_unit(value: float) -> float:
    """Round one stored coordinate to 6 decimals, normalizing negative zero.

    Args:
        value: Coordinate value.

    Returns:
        The rounded value.
    """
    rounded = round(value, 6)
    return 0.0 if rounded == 0.0 else rounded


def emit_glyph(paths: Sequence[StrokePath], transform: GlyphTransform) -> str:
    """Emit one glyph as a self-contained normalized HPGL command string.

    Raw coordinates are mapped through ``transform`` as they are written, so
    no intermediate :class:`~plt_optimizer.core.models.Coordinate` is built
    (that type rounds to 3 decimals, which would silently truncate the
    4-decimal output). Segments are written tip-to-tail, but the parser records
    paths whose segments do not always chain (a mid-path ``PU`` re-positions
    the pen before an arc without opening a new path), so a fresh ``PU`` is
    emitted whenever a segment starts away from the current pen position -
    mirroring how EngraveLab precedes each ``PD;AA`` arc with its own ``PU``.
    The transform reflects Y, so every arc sweep angle is negated to keep the
    cut direction. The string ends with a semicolon and re-parses cleanly with
    :class:`~plt_optimizer.core.parser.PLTParser`.

    Args:
        paths: The glyph's raw stroke paths.
        transform: Normalization to apply while writing.

    Returns:
        HPGL text such as ``"PU0.0000,0.0000;PD10.0000,0.0000;"``, or ``""``
        when the glyph carries no geometry.
    """
    parts: List[str] = []
    for path in paths:
        if not path.segments:
            continue
        current = (
            path.pen_up_position if path.pen_up_position is not None else path.segments[0].start
        )
        start_x, start_y = transform.apply(current.x, current.y)
        parts.append(f"PU{_format_number(start_x)},{_format_number(start_y)}")
        for segment in path.segments:
            if not (
                math.isclose(segment.start.x, current.x, abs_tol=1e-3)
                and math.isclose(segment.start.y, current.y, abs_tol=1e-3)
            ):
                break_x, break_y = transform.apply(segment.start.x, segment.start.y)
                parts.append(f"PU{_format_number(break_x)},{_format_number(break_y)}")
            end_x, end_y = transform.apply(segment.end.x, segment.end.y)
            cmd = "PD" if segment.is_cutting else "PU"
            if isinstance(segment, ArcSegment):
                center_x, center_y = transform.apply(segment.center.x, segment.center.y)
                parts.append(
                    f"{cmd};AA{_format_number(center_x)},{_format_number(center_y)},"
                    f"{_format_number(-segment.sweep_angle)}"
                )
            else:
                parts.append(f"{cmd}{_format_number(end_x)},{_format_number(end_y)}")
            current = segment.end
    if not parts:
        return ""
    return ";".join(parts) + ";"


def _angle_in_sweep(theta: float, theta_start: float, theta_end: float) -> bool:
    """Return whether ``theta`` lies inside the arc's swept angular interval.

    The swept set is the closed interval between the start and end angles (the
    same convention :func:`arc_bounds` uses). Angles are tested modulo 2*pi so
    arcs crossing the negative X axis work unchanged.

    Args:
        theta: Candidate angle, in radians.
        theta_start: Arc start angle, in radians.
        theta_end: Arc end angle, in radians.

    Returns:
        True when ``theta`` is swept by the arc.
    """
    lo, hi = min(theta_start, theta_end), max(theta_start, theta_end)
    full_turn = 2.0 * math.pi
    for shift in (-1.0, 0.0, 1.0):
        shifted = theta + shift * full_turn
        if lo - 1e-9 <= shifted <= hi + 1e-9:
            return True
    return False


def _line_xs_at(line: Tuple[float, float, float, float], y: float) -> List[float]:
    """Return the X values where a horizontal line crosses one polyline segment.

    Args:
        line: ``(x0, y0, x1, y1)`` in normalized units.
        y: Sample height.

    Returns:
        Zero or one crossing for a slanted/vertical segment, or both endpoints
        for a horizontal segment lying exactly on ``y`` (its full extent is
        "on" the sample line).
    """
    x0, y0, x1, y1 = line
    if abs(y1 - y0) <= _EPS:
        return [x0, x1] if abs(y - y0) <= _EPS else []
    lo, hi = min(y0, y1), max(y0, y1)
    if y < lo - _EPS or y > hi + _EPS:
        return []
    return [x0 + (y - y0) * (x1 - x0) / (y1 - y0)]


def _arc_xs_at(
    center_x: float,
    center_y: float,
    radius: float,
    theta_start: float,
    theta_end: float,
    y: float,
) -> List[float]:
    """Return the X values where a horizontal line crosses a swept arc.

    The circle/line intersection is solved analytically (no chord flattening),
    then each candidate is kept only if its angle is inside the swept interval.

    Args:
        center_x: Arc center X in normalized units.
        center_y: Arc center Y in normalized units.
        radius: Arc radius in normalized units.
        theta_start: Arc start angle, in radians.
        theta_end: Arc end angle, in radians.
        y: Sample height.

    Returns:
        Zero, one (tangent) or two crossing X values.
    """
    if abs(radius) <= _EPS:
        return []
    delta_y = y - center_y
    if abs(delta_y) > radius + _EPS:
        return []
    discriminant = radius * radius - delta_y * delta_y
    if discriminant < 0.0:
        return []
    delta_x = math.sqrt(discriminant)
    found: List[float] = []
    for sign in (-1.0, 1.0):
        if abs(delta_x) <= _EPS and found:
            break  # tangent: the two candidates coincide
        offset = sign * delta_x
        if _angle_in_sweep(math.atan2(delta_y, offset), theta_start, theta_end):
            found.append(center_x + offset)
    return found


def _normalized_geometry(
    paths: Sequence[StrokePath],
    transform: GlyphTransform,
) -> Tuple[
    List[Tuple[float, float, float, float]],
    List[Tuple[float, float, float, float, float]],
]:
    """Map raw paths into float primitives usable for horizontal sampling.

    Args:
        paths: The glyph's raw stroke paths.
        transform: Normalization to apply.

    Returns:
        ``(lines, arcs)`` where lines are ``(x0, y0, x1, y1)`` and arcs are
        ``(center_x, center_y, radius, theta_start, theta_end)``. Arc radii
        scale uniformly and the sweep interval is re-derived from the mapped
        endpoints (with the Y reflection's negated sweep), exactly as a
        consumer parsing the emitted glyph would.
    """
    lines: List[Tuple[float, float, float, float]] = []
    arcs: List[Tuple[float, float, float, float, float]] = []
    for path in paths:
        for segment in path.segments:
            start_x, start_y = transform.apply(segment.start.x, segment.start.y)
            end_x, end_y = transform.apply(segment.end.x, segment.end.y)
            if isinstance(segment, ArcSegment):
                center_x, center_y = transform.apply(segment.center.x, segment.center.y)
                radius = transform.scale * segment.radius
                theta_start = math.atan2(start_y - center_y, start_x - center_x)
                theta_end = theta_start + math.radians(-segment.sweep_angle)
                arcs.append((center_x, center_y, radius, theta_start, theta_end))
            else:
                lines.append((start_x, start_y, end_x, end_y))
    return lines, arcs


def _interpolate_gaps(samples: Sequence[Optional[float]]) -> List[float]:
    """Fill undefined envelope samples by linear interpolation.

    A sampling line that misses the glyph entirely (it passed through a gap
    between strokes) takes the value interpolated between the nearest defined
    samples on either side; when only one side is defined, the value is clamped
    to it. At least one defined sample must exist.

    Args:
        samples: Per-height envelope X values, ``None`` where undefined.

    Returns:
        One value per input sample, with gaps filled.

    Raises:
        ValueError: If every sample is undefined.
    """
    defined = [index for index, value in enumerate(samples) if value is not None]
    if not defined:
        raise ValueError("Cannot interpolate an envelope with no defined samples")
    filled: List[float] = []
    for index, value in enumerate(samples):
        if value is not None:
            filled.append(value)
            continue
        below = max((i for i in defined if i < index), default=None)
        above = min((i for i in defined if i > index), default=None)
        if below is not None and above is not None:
            low_value = samples[below] or 0.0
            high_value = samples[above] or 0.0
            weight = (index - below) / (above - below)
            filled.append(low_value + (high_value - low_value) * weight)
        else:
            nearest = below if below is not None else above
            assert nearest is not None  # guaranteed: `defined` is non-empty
            filled.append(samples[nearest] or 0.0)
    return filled


def sample_envelopes(
    paths: Sequence[StrokePath],
    transform: GlyphTransform,
    bounds: Bounds,
    samples: int,
) -> Tuple[List[List[float]], List[List[float]]]:
    """Extract the left and right profile envelopes of one glyph.

    The glyph's vertical extent is sampled on ``samples`` uniform heights and,
    at each height, every polyline segment and swept arc is intersected
    analytically with the horizontal sample line. The left envelope records the
    smallest crossing X, the right envelope the largest, which gives the
    typesetter a true air-gap profile instead of a fixed advance width. Heights
    that miss the glyph entirely are interpolated from their neighbours (see
    :func:`_interpolate_gaps`).

    Args:
        paths: The glyph's raw stroke paths.
        transform: Normalization to apply.
        bounds: The glyph's normalized bounding box.
        samples: Number of sample heights (``>= 2``).

    Returns:
        ``(left_envelope, right_envelope)``, each a list of ``[x, y]`` pairs in
        normalized plotter units (6-decimal rounded), or ``([], [])`` when the
        glyph has no geometry.

    Raises:
        ValueError: If ``samples`` is less than 2.
    """
    if samples < 2:
        raise ValueError("Envelope sampling requires at least 2 samples")
    lines, arcs = _normalized_geometry(paths, transform)
    if not lines and not arcs:
        return [], []

    low_y, high_y = bounds[1], bounds[3]
    span = high_y - low_y
    if span <= _EPS:
        heights = [low_y] * samples
    else:
        step = span / (samples - 1)
        heights = [low_y + index * step for index in range(samples)]
        heights[-1] = high_y

    left_gaps: List[Optional[float]] = []
    right_gaps: List[Optional[float]] = []
    for y in heights:
        crossings: List[float] = []
        for line in lines:
            crossings.extend(_line_xs_at(line, y))
        for center_x, center_y, radius, theta_start, theta_end in arcs:
            crossings.extend(_arc_xs_at(center_x, center_y, radius, theta_start, theta_end, y))
        left_gaps.append(min(crossings) if crossings else None)
        right_gaps.append(max(crossings) if crossings else None)

    left = _interpolate_gaps(left_gaps)
    right = _interpolate_gaps(right_gaps)
    left_envelope = [[_round_unit(x), _round_unit(y)] for x, y in zip(left, heights)]
    right_envelope = [[_round_unit(x), _round_unit(y)] for x, y in zip(right, heights)]
    return left_envelope, right_envelope


def build_character_entry(
    cluster: Cluster,
    baseline: float,
    scale: float,
    samples: int,
) -> CharacterEntry:
    """Build the JSON entry for one character.

    Args:
        cluster: The glyph's raw cluster.
        baseline: The row's raw baseline Y, in plotter units.
        scale: Global scale factor ``S``.
        samples: Envelope sample count.

    Returns:
        A ``{"bounding_box", "left_envelope", "right_envelope", "glyph"}``
        mapping in normalized plotter units (+Y up, baseline ``y = 0``, left
        edge ``x = 0``).
    """
    raw_bounds = cluster.bounds
    transform = GlyphTransform(scale=scale, origin_x=raw_bounds[0], baseline=baseline)
    bounds = transform.map_bounds(raw_bounds)
    left_envelope, right_envelope = sample_envelopes(cluster.paths, transform, bounds, samples)
    glyph = emit_glyph(cluster.paths, transform)
    if not glyph:
        logger.warning("Character produced no geometry at raw bounds %s", raw_bounds)
    elif (bounds[2] - bounds[0]) < 1e-9 and (bounds[3] - bounds[1]) < 1e-9:
        logger.warning("Character is a degenerate point at raw bounds %s", raw_bounds)
    return {
        "bounding_box": {
            "min_x": _round_unit(bounds[0]),
            "max_x": _round_unit(bounds[2]),
            "min_y": _round_unit(bounds[1]),
            "max_y": _round_unit(bounds[3]),
        },
        "left_envelope": left_envelope,
        "right_envelope": right_envelope,
        "glyph": glyph,
    }


def extract_font_from_document(
    document: PLTDocument,
    characters: Sequence[str],
    reference_char: str,
    declared_height: float,
    row_threshold: Optional[float] = None,
    cluster_threshold: Optional[float] = None,
    envelope_samples: int = ENVELOPE_SAMPLES,
) -> Tuple[FontEntry, int, float, float]:
    """Split one engraved sheet into a font entry.

    Args:
        document: The parsed sample sheet.
        characters: Expected payload characters, in reading order.
        reference_char: Row-framing reference character from the file name.
        declared_height: Height parsed from the file name (metadata only).
        row_threshold: Manual Y-gap row threshold; ``None`` searches.
        cluster_threshold: Manual X-gap glyph threshold; ``None`` searches.
        envelope_samples: Envelope sample count per glyph.

    Returns:
        ``(font_entry, row_count, row_threshold, cluster_threshold)`` where
        ``font_entry`` is the JSON-ready font mapping (without ``file_path``).

    Raises:
        FontExtractionError: If the layout cannot be verified or the reference
            height cannot be measured.
    """
    paths = [p for p in document.stroke_paths if p.segments]
    rows, used_row_threshold, used_cluster_threshold = find_layout(
        paths, characters, reference_char, row_threshold, cluster_threshold
    )

    reference_height_raw = median([row.reference_height for row in rows])
    if reference_height_raw <= _EPS:
        raise FontExtractionError(
            f"Reference character {reference_char!r} measures {reference_height_raw:.6f} units "
            f"tall; it cannot define the normalization scale. Choose a reference character "
            f"with real height."
        )
    scale = UNITS_PER_INCH / reference_height_raw
    reference_height_in = reference_height_raw / UNITS_PER_INCH

    characters_out: Dict[str, CharacterEntry] = {}
    index = 0
    for row in rows:
        for cluster in row.content:
            character = characters[index]
            index += 1
            characters_out[character] = build_character_entry(
                cluster, row.baseline, scale, envelope_samples
            )

    entry: FontEntry = {
        "reference_char": reference_char,
        "declared_height_in": declared_height,
        "reference_char_height_in": _round_unit(reference_height_in),
        "normalized_ref_height": 1.0,
        "characters": characters_out,
    }
    logger.info(
        "Median glyph height above baseline %.3f in after scaling "
        "(reference char %r measures %.4f in, design ref height is 1.000 in)",
        median(
            [
                (row.baseline - cluster.bounds[1]) * scale / UNITS_PER_INCH
                for row in rows
                for cluster in row.content
            ]
        ),
        reference_char,
        reference_height_in,
    )
    return entry, len(rows), used_row_threshold, used_cluster_threshold


def _repo_relative(path: Path) -> str:
    """Return ``path`` as a POSIX string relative to the repository root.

    Args:
        path: Any path, absolute or relative.

    Returns:
        The repo-relative POSIX path when ``path`` lives under the repository,
        otherwise the given path in POSIX form.
    """
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def extract_font_file(
    plt_path: Path,
    characters: Sequence[str],
    font_name: Optional[str] = None,
    row_threshold: Optional[float] = None,
    cluster_threshold: Optional[float] = None,
    envelope_samples: int = ENVELOPE_SAMPLES,
) -> FontExtraction:
    """Extract one ``<font>_<height>_<ref>.plt`` sample sheet into a font entry.

    Args:
        plt_path: Path to the engraved sample PLT.
        characters: Expected payload characters, in reading order.
        font_name: Explicit font key; defaults to the file-name font part.
        row_threshold: Manual Y-gap row threshold (see
            :func:`extract_font_from_document`).
        cluster_threshold: Manual X-gap glyph threshold.
        envelope_samples: Envelope sample count per glyph.

    Returns:
        The extraction result.

    Raises:
        FontExtractionError: If the file name is malformed, the PLT cannot be
            parsed, or the sheet's layout fails verification.
    """
    default_name, declared_height, reference_char = parse_font_file_name(plt_path)
    name = font_name or default_name
    parser = PLTParser()
    try:
        document = parser.parse_file(plt_path)
    except ParseError as e:
        raise FontExtractionError(f"Failed to parse {plt_path}: {e}") from e

    entry, row_count, used_row, used_cluster = extract_font_from_document(
        document,
        characters,
        reference_char,
        declared_height,
        row_threshold,
        cluster_threshold,
        envelope_samples,
    )
    check_height_drift(
        name,
        plt_path,
        declared_height,
        reference_char,
        entry["reference_char_height_in"],
    )
    full_entry: FontEntry = {"file_path": _repo_relative(plt_path), **entry}
    logger.info(
        "Extracted font %r from %s: %d glyphs across %d rows, "
        "row_threshold=%.1f units, cluster_threshold=%.1f units, "
        "declared height=%.4g in, measured reference height=%.6g in",
        name,
        plt_path.name,
        len(entry["characters"]),
        row_count,
        used_row,
        used_cluster,
        declared_height,
        entry["reference_char_height_in"],
    )
    return FontExtraction(
        font_name=name,
        entry=full_entry,
        row_count=row_count,
        row_threshold=used_row,
        cluster_threshold=used_cluster,
    )


def _is_font_entry(entry: object) -> bool:
    """Return whether a JSON value looks like a new-format font entry.

    Args:
        entry: A value from the top level of ``plt_fonts.json``.

    Returns:
        True when the value carries the nested ``characters`` mapping.
    """
    return isinstance(entry, dict) and "characters" in entry


def load_existing_fonts(output: Path) -> Dict[str, FontEntry]:
    """Load the existing ``plt_fonts.json`` for merging.

    Args:
        output: Path to the JSON font dictionary.

    Returns:
        Mapping of font name to font entry; empty when the file is absent.

    Raises:
        FontExtractionError: If the file is not a JSON object of new-format
            font entries, or still carries the pre-envelope flat
            ``char -> HPGL`` layout (which cannot be merged into the new
            schema and must be regenerated).
    """
    if not output.exists():
        return {}
    try:
        raw = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise FontExtractionError(f"Failed to read existing font file {output}: {e}") from e
    if not isinstance(raw, dict):
        raise FontExtractionError(f"Font file {output} must contain a JSON object")
    legacy = [name for name, entry in raw.items() if not _is_font_entry(entry)]
    if legacy:
        raise FontExtractionError(
            f"Font file {output} carries the legacy flat schema for "
            f"{', '.join(sorted(str(name) for name in legacy))}; re-run with --rebuild "
            f"to regenerate it from the sample sheets."
        )
    fonts: Dict[str, FontEntry] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not isinstance(entry.get("characters"), dict):
            raise FontExtractionError(
                f"Font file {output} must map font names to objects with a "
                f"'characters' object of per-character entries"
            )
        fonts[name] = dict(entry)
    return fonts


def write_fonts_json(output: Path, fonts: Dict[str, FontEntry], characters: Sequence[str]) -> None:
    """Write the merged font dictionary deterministically.

    Font keys are sorted alphabetically; within each font the characters follow
    the ``ascii.txt`` order (any other keys are appended sorted).

    Args:
        output: Destination path (parents are created).
        fonts: Complete mapping of font name to font entry.
        characters: Canonical character order.
    """
    ordered: Dict[str, FontEntry] = {}
    for name in sorted(fonts):
        entry = dict(fonts[name])
        glyphs = entry.get("characters")
        if isinstance(glyphs, dict):
            merged: Dict[str, Any] = {c: glyphs[c] for c in characters if c in glyphs}
            for key in sorted(set(glyphs) - set(merged)):
                merged[key] = glyphs[key]
            entry["characters"] = merged
        ordered[name] = entry
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(ordered, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        raise FontExtractionError(f"Failed to write font file {output}: {e}") from e
    logger.info("Wrote %d font(s) to %s", len(ordered), output)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        prog="extract_plt_fonts",
        description=(
            "Extract baseline-normalized per-character HPGL glyphs and left/right "
            "profile envelopes from engraved, reference-framed ASCII sample sheets "
            "(Fonts/PLT/*.plt) into plt_fonts.json."
        ),
        epilog=(
            "Typical usage:\n"
            "  uv run python Fonts/extract_plt_fonts.py\n"
            "  uv run python Fonts/extract_plt_fonts.py --font-name Dino "
            "--fonts-dir Fonts/PLT -v\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--fonts-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help="Directory containing '<font>_<height in>_<ref char>.plt' sample "
        "sheets (default: %(default)s relative to the repo root).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="JSON font dictionary to create/update (default: %(default)s).",
    )
    parser.add_argument(
        "--ascii-file",
        type=Path,
        default=DEFAULT_ASCII_FILE,
        help="Ordered character list (default: %(default)s).",
    )
    parser.add_argument(
        "--row-threshold",
        type=float,
        default=None,
        help="Manual Y-gap row-splitting threshold in plotter units "
        "(1000 units = 1 inch). Default: auto-searched so the rows reproduce "
        "the character list and the framing geometry matches.",
    )
    parser.add_argument(
        "--cluster-threshold",
        type=float,
        default=None,
        help="Manual X-gap clustering threshold in plotter units "
        "(1000 units = 1 inch). Default: auto-searched so the rows reproduce "
        "the character list and the framing geometry matches.",
    )
    parser.add_argument(
        "--envelope-samples",
        type=int,
        default=ENVELOPE_SAMPLES,
        help="Number of vertical samples per left/right profile envelope (default: %(default)s).",
    )
    parser.add_argument(
        "--font-name",
        default=None,
        help="Explicit font key; only valid with a single input file. "
        "Default: the file-name font part with underscores as spaces, "
        "title-cased ('dino_0.5_E.plt' -> 'Dino').",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Regenerate the output from scratch, dropping fonts not present "
        "in the input directory. Default: merge, replacing only extracted keys.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the font extraction CLI.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code: 0 on full success, 1 when any step failed.
    """
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s | %(message)s",
    )

    if args.envelope_samples < 2:
        logger.error("--envelope-samples must be at least 2")
        return 1

    try:
        characters = load_characters(args.ascii_file)
    except FontExtractionError as e:
        logger.error("%s", e)
        return 1
    logger.debug("Expected characters (%d): %s", len(characters), "".join(characters))

    if not args.fonts_dir.is_dir():
        logger.error("Fonts directory not found: %s", args.fonts_dir)
        return 1
    plt_files = sorted(p for p in args.fonts_dir.iterdir() if p.suffix.lower() == ".plt")
    if not plt_files:
        logger.error("No .plt sample sheets found in %s", args.fonts_dir)
        return 1
    if args.font_name and len(plt_files) != 1:
        logger.error("--font-name requires exactly one input file (found %d)", len(plt_files))
        return 1

    extractions: List[FontExtraction] = []
    failed = False
    for plt_path in plt_files:
        try:
            extraction = extract_font_file(
                plt_path,
                characters,
                font_name=args.font_name,
                row_threshold=args.row_threshold,
                cluster_threshold=args.cluster_threshold,
                envelope_samples=args.envelope_samples,
            )
        except FontExtractionError as e:
            logger.error("%s", e)
            failed = True
            continue
        extractions.append(extraction)

    if not extractions:
        logger.error("No fonts extracted; %s left unchanged", args.output)
        return 1

    try:
        fonts = {} if args.rebuild else load_existing_fonts(args.output)
    except FontExtractionError as e:
        logger.error("%s", e)
        return 1
    for extraction in extractions:
        fonts[extraction.font_name] = extraction.entry
    try:
        write_fonts_json(args.output, fonts, characters)
    except FontExtractionError as e:
        logger.error("%s", e)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
