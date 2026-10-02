#!/usr/bin/env python3
"""Reverse-engineer PLT fonts from engraved ASCII sample sheets.

EngraveLab / Vision Pro cannot export their vector fonts, but they can
*engrave* them. The workflow (see ``How to extract fonts from EngraveLab or
VisionPro.md`` beside this script) is:

1. In EngraveLab/Vision Pro, use the **Text Compose** tool (never *Frame
   Text Compose*, which compresses to fit the plate) with the contents of
   ``Fonts/ascii.txt``: every printable ASCII character in one long row,
   separated by many spaces, any font. Pick a text height small enough that
   the whole row fits the machine plate without EngraveLab compressing the
   toolpath (e.g. 0.05 inch); the height is recorded in the file name.
2. Engrave the sheet to a PLT file.
3. Drop the PLT into ``Fonts/PLT-ascii/`` named ``<font name> <height>.plt``
   (e.g. ``dino 0.05.plt``), where ``<height>`` is the engraved text height
   in inches.
4. Run this script::

       uv run python Fonts/extract_plt_fonts.py

Each input file is parsed with the core :class:`~plt_optimizer.core.parser.PLTParser`,
split into individual glyphs by clustering stroke paths along X (the
character spacing is intentionally huge, but EngraveLab emits strokes in a
scrambled order, so chronological chunking cannot be used), translated so
each glyph is centered on the origin, scaled by ``1 / text height`` to a
uniform 1.0-inch design height, and re-emitted as a self-contained
``PU``/``PD``/``AA`` command string (3-decimal plotter units, 1000 units =
1 inch).

The result is merged into ``Fonts/plt_fonts.json``::

    {"<Font Name>": {"<char>": "<HPGL commands>", ...}, ...}

Font keys are the file-name font part title-cased (``dino 0.05.plt`` ->
``"Dino"``); use ``--font-name`` to override for a single file. Existing
fonts in the JSON are preserved unless ``--rebuild`` is given. The
downstream consumer (placing/scaling glyphs when generating labels) is a
later task.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow running as a plain script
    sys.path.insert(0, str(REPO_ROOT))

from plt_optimizer.core.models import (  # noqa: E402
    ArcSegment,
    Coordinate,
    PLTDocument,
    Segment,
    StrokePath,
    StrokeSegment,
)
from plt_optimizer.core.parser import ParseError, PLTParser  # noqa: E402

logger = logging.getLogger("extract_plt_fonts")

FONTS_DIR = REPO_ROOT / "Fonts"
DEFAULT_INPUT_DIR = FONTS_DIR / "PLT-ascii"
DEFAULT_OUTPUT = FONTS_DIR / "plt_fonts.json"
DEFAULT_ASCII_FILE = FONTS_DIR / "ascii.txt"

# Bounding-box type: (x_min, y_min, x_max, y_max) in plotter units.
Bounds = Tuple[float, float, float, float]

# A font's extracted glyph map: character -> origin-centered HPGL string.
GlyphMap = Dict[str, str]


class FontExtractionError(Exception):
    """Raised when a PLT sample sheet cannot be extracted into a font."""


@dataclass(frozen=True)
class Interval:
    """A closed X interval ``[x_min, x_max]`` occupied by one stroke path.

    Attributes:
        x_min: Left-most X coordinate (plotter units).
        x_max: Right-most X coordinate (plotter units).
    """

    x_min: float
    x_max: float


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
    def path_count(self) -> int:
        """Return the number of stroke paths in this cluster."""
        return len(self.paths)

    @property
    def center_x(self) -> float:
        """Return the X center of the cluster bounding box."""
        return (self.bounds[0] + self.bounds[2]) / 2.0


@dataclass(frozen=True)
class FontExtraction:
    """The result of extracting one PLT sample sheet.

    Attributes:
        font_name: Title-cased font key used in ``plt_fonts.json``.
        glyphs: Character -> origin-centered HPGL command string (scaled to
            1.0-inch design height).
        threshold: X-gap clustering threshold actually used (plotter units).
        text_height: Engraved text height in inches parsed from the file
            name; glyphs were scaled by ``1 / text_height``.
    """

    font_name: str
    glyphs: GlyphMap
    threshold: float
    text_height: float


def load_characters(ascii_file: Path) -> List[str]:
    """Load the ordered printable-ASCII character list.

    Every whitespace run in the file is a separator; the remaining
    characters, in file order, are the expected glyph sequence.

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


def parse_font_file_name(plt_path: Path) -> Tuple[str, float]:
    """Split an engraved sample file name into (font name, text height).

    The convention is ``<font name> <text height>.plt`` (e.g.
    ``dino 0.05.plt``): the last whitespace-separated token of the stem is
    the text height in inches engraved in EngraveLab/Vision Pro, and the
    remaining tokens form the font name (title-cased). The extractor scales
    every glyph by ``1 / text height`` so the stored font is normalized to
    1.0-inch design height regardless of the engraved size.

    Args:
        plt_path: Path to the engraved sample PLT.

    Returns:
        Tuple of ``(font_name, text_height_inches)``.

    Raises:
        FontExtractionError: If the file name carries no positive finite
            trailing text-height token.
    """
    tokens = plt_path.stem.split()
    if len(tokens) < 2:
        raise FontExtractionError(
            f"{plt_path.name}: file name must be '<font name> <text height>.plt' "
            f"(e.g. 'dino 0.05.plt'); no text height found"
        )
    *name_tokens, height_token = tokens
    try:
        text_height = float(height_token)
    except ValueError as e:
        raise FontExtractionError(
            f"{plt_path.name}: trailing file-name token {height_token!r} is not a "
            f"text height in inches (e.g. 'dino 0.05.plt')"
        ) from e
    if not math.isfinite(text_height) or text_height <= 0.0:
        raise FontExtractionError(
            f"{plt_path.name}: text height must be a positive finite number of inches"
        )
    return " ".join(name_tokens).title(), text_height


def arc_bounds(arc: ArcSegment) -> Bounds:
    """Return the bounding box of the arc's *swept* portion.

    EngraveLab approximates many near-straight glyph strokes as huge-radius
    best-fit arcs, so the arc's full circle dwarfs the actual cut. Measuring
    only the swept extent (the two endpoints plus any cardinal angle the arc
    passes through) keeps a glyph's footprint tight and lets wide-spaced
    characters separate cleanly. A full-revolution arc (e.g. an ``CI`` circle)
    still yields its whole circle.

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
    boxes = [segment_bounds(seg) for seg in path.segments]
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def merge_intervals(intervals: Sequence[Interval]) -> List[Interval]:
    """Merge overlapping or touching X intervals.

    Single-linkage merge at zero gap: sorted intervals are fused whenever
    the next one starts at or before the running right edge. The result is
    a disjoint, sorted list whose consecutive gaps are all strictly positive.

    Args:
        intervals: Arbitrary-order X intervals.

    Returns:
        Disjoint sorted intervals covering the same span.
    """
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: (iv.x_min, iv.x_max))
    merged: List[Interval] = [ordered[0]]
    for current in ordered[1:]:
        last = merged[-1]
        if current.x_min <= last.x_max:
            merged[-1] = Interval(last.x_min, max(last.x_max, current.x_max))
        else:
            merged.append(current)
    return merged


def auto_cluster_threshold(intervals: Sequence[Interval], expected_clusters: int) -> float:
    """Pick the X-gap threshold that yields exactly ``expected_clusters``.

    With the intervals merged at zero gap, every consecutive gap is positive
    and a threshold ``t`` cuts exactly the gaps larger than ``t``. To obtain
    ``k`` clusters, the ``k - 1`` largest gaps become cuts; the returned
    threshold is the midpoint between the smallest cut gap and the largest
    non-cut gap (0.0 when every gap is a cut), i.e. the widest-margin choice.

    Args:
        intervals: Arbitrary-order X intervals (one per stroke path).
        expected_clusters: Required cluster count (characters on the sheet).

    Returns:
        Threshold in plotter units; clustering merges gaps ``<= t``.

    Raises:
        FontExtractionError: If there are too few intervals, or the two
            boundary gaps tie and the split is ambiguous.
    """
    merged = merge_intervals(intervals)
    gaps = sorted(nxt.x_min - cur.x_max for cur, nxt in zip(merged, merged[1:]))
    needed_cuts = expected_clusters - 1
    if len(gaps) < needed_cuts:
        raise FontExtractionError(
            f"Found only {len(merged)} separated stroke groups; need at least "
            f"{expected_clusters} to match the character list. Check that the "
            f"sheet is a single row of characters separated by wide spaces."
        )
    smallest_cut = gaps[len(gaps) - needed_cuts]
    largest_kept = gaps[len(gaps) - needed_cuts - 1] if len(gaps) > needed_cuts else 0.0
    if smallest_cut <= largest_kept:  # pragma: no cover - guarded by ordering
        raise FontExtractionError(
            f"Cannot separate {expected_clusters} glyphs: tied inter-character "
            f"gaps ({largest_kept:.3f} vs {smallest_cut:.3f})."
        )
    return (largest_kept + smallest_cut) / 2.0


def cluster_paths(paths: Sequence[StrokePath], threshold: float) -> List[Cluster]:
    """Group stroke paths into glyph clusters along X.

    Paths are sorted by left edge and single-linkage merged: a path joins the
    current cluster while its ``x_min`` is within ``threshold`` of the
    running right edge (gaps ``<= threshold`` merge, larger gaps cut).
    EngraveLab emits strokes in a scrambled order, so spatial clustering -
    not the chronological :class:`~plt_optimizer.core.chunker.Chunker` -
    is the reliable split here.

    Args:
        paths: Stroke paths of one document (order-independent).
        threshold: Maximum intra-glyph X gap, in plotter units.

    Returns:
        Clusters sorted left-to-right by bounding-box center X. Paths
        without segments are skipped.

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
        bounds: Bounds = (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )
        clusters.append(Cluster(paths=tuple(group_paths), bounds=bounds))

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


def translate_coordinate(coordinate: Coordinate, dx: float, dy: float) -> Coordinate:
    """Return ``coordinate`` shifted by ``(dx, dy)``.

    Args:
        coordinate: Point to shift.
        dx: X offset in plotter units.
        dy: Y offset in plotter units.

    Returns:
        A new :class:`Coordinate` (constructor rounds to 3 decimals).
    """
    return Coordinate(coordinate.x + dx, coordinate.y + dy)


def translate_segment(segment: Segment, dx: float, dy: float) -> Segment:
    """Return ``segment`` shifted by ``(dx, dy)``.

    Arc centers move like any point and the sweep angle is preserved (a pure
    translation leaves orientation untouched).

    Args:
        segment: Line or arc segment to shift.
        dx: X offset in plotter units.
        dy: Y offset in plotter units.

    Returns:
        A new segment of the same kind.
    """
    if isinstance(segment, ArcSegment):
        return ArcSegment(
            start=translate_coordinate(segment.start, dx, dy),
            end=translate_coordinate(segment.end, dx, dy),
            center=translate_coordinate(segment.center, dx, dy),
            sweep_angle=segment.sweep_angle,
            is_cutting=segment.is_cutting,
        )
    return StrokeSegment(
        start=translate_coordinate(segment.start, dx, dy),
        end=translate_coordinate(segment.end, dx, dy),
        is_cutting=segment.is_cutting,
    )


def translate_path(path: StrokePath, dx: float, dy: float) -> StrokePath:
    """Return ``path`` shifted by ``(dx, dy)``.

    Args:
        path: Path to shift.
        dx: X offset in plotter units.
        dy: Y offset in plotter units.

    Returns:
        A new :class:`StrokePath` with shifted pen-up position and segments.
    """
    pen_up = (
        translate_coordinate(path.pen_up_position, dx, dy)
        if path.pen_up_position is not None
        else None
    )
    return StrokePath(
        pen_up_position=pen_up,
        segments=tuple(translate_segment(seg, dx, dy) for seg in path.segments),
    )


def scale_coordinate(coordinate: Coordinate, factor: float) -> Coordinate:
    """Return ``coordinate`` scaled uniformly about the origin.

    Args:
        coordinate: Point to scale.
        factor: Uniform scale factor.

    Returns:
        A new :class:`Coordinate` (constructor rounds to 3 decimals).
    """
    return Coordinate(coordinate.x * factor, coordinate.y * factor)


def scale_segment(segment: Segment, factor: float) -> Segment:
    """Return ``segment`` scaled uniformly about the origin.

    A uniform scale is a similarity transform: arc centers scale like any
    point, radii scale with the factor, and sweep angles are preserved.

    Args:
        segment: Line or arc segment to scale.
        factor: Uniform scale factor.

    Returns:
        A new segment of the same kind.
    """
    if isinstance(segment, ArcSegment):
        return ArcSegment(
            start=scale_coordinate(segment.start, factor),
            end=scale_coordinate(segment.end, factor),
            center=scale_coordinate(segment.center, factor),
            sweep_angle=segment.sweep_angle,
            is_cutting=segment.is_cutting,
        )
    return StrokeSegment(
        start=scale_coordinate(segment.start, factor),
        end=scale_coordinate(segment.end, factor),
        is_cutting=segment.is_cutting,
    )


def scale_path(path: StrokePath, factor: float) -> StrokePath:
    """Return ``path`` scaled uniformly about the origin.

    Args:
        path: Path to scale.
        factor: Uniform scale factor.

    Returns:
        A new :class:`StrokePath`.
    """
    pen_up = (
        scale_coordinate(path.pen_up_position, factor) if path.pen_up_position is not None else None
    )
    return StrokePath(
        pen_up_position=pen_up,
        segments=tuple(scale_segment(seg, factor) for seg in path.segments),
    )


def _format_number(value: float) -> str:
    """Format one coordinate value with the source files' 3-decimal precision.

    Args:
        value: Numeric value in plotter units.

    Returns:
        Fixed-point string, e.g. ``"1234.567"``.
    """
    return f"{value:.3f}"


def format_segment(segment: Segment) -> str:
    """Format one segment as HPGL command text (no trailing semicolon).

    Mirrors ``plt_optimizer.generate.plate_optimizer._format_segment`` but
    keeps 3-decimal floats instead of integer units.

    Args:
        segment: Line or arc segment.

    Returns:
        e.g. ``"PD123.456,78.900"`` or ``"PD;AA10.000,20.000,-90.000"``.
    """
    cmd = "PD" if segment.is_cutting else "PU"
    if isinstance(segment, ArcSegment):
        return (
            f"{cmd};AA{_format_number(segment.center.x)},"
            f"{_format_number(segment.center.y)},{_format_number(segment.sweep_angle)}"
        )
    return f"{cmd}{_format_number(segment.end.x)},{_format_number(segment.end.y)}"


def emit_glyph(paths: Sequence[StrokePath]) -> str:
    """Emit glyph paths as one self-contained HPGL command string.

    Segments are written tip-to-tail, but the parser records paths whose
    segments do not always chain (a mid-path ``PU`` re-positions the pen
    before an arc without opening a new path). To round-trip faithfully, a
    fresh ``PU`` is emitted whenever a segment starts away from the current
    pen position - mirroring how EngraveLab precedes each ``PD;AA`` arc with
    its own ``PU``. Every emitted command keeps 3-decimal precision and the
    string ends with a semicolon, so it re-parses cleanly with
    :class:`~plt_optimizer.core.parser.PLTParser`.

    Args:
        paths: Origin-centered glyph paths.

    Returns:
        HPGL text such as ``"PU0.000,0.000;PD10.000,0.000;"``, or ``""`` when
        the glyph carries no geometry.
    """
    parts: List[str] = []
    for path in paths:
        if not path.segments:
            continue
        current = (
            path.pen_up_position if path.pen_up_position is not None else path.segments[0].start
        )
        parts.append(f"PU{_format_number(current.x)},{_format_number(current.y)}")
        for segment in path.segments:
            if not (
                math.isclose(segment.start.x, current.x, abs_tol=1e-3)
                and math.isclose(segment.start.y, current.y, abs_tol=1e-3)
            ):
                parts.append(
                    f"PU{_format_number(segment.start.x)},{_format_number(segment.start.y)}"
                )
            parts.append(format_segment(segment))
            current = segment.end
    if not parts:
        return ""
    return ";".join(parts) + ";"


def extract_font_from_document(
    document: PLTDocument,
    characters: Sequence[str],
    cluster_threshold: Optional[float] = None,
    scale: float = 1.0,
) -> Tuple[GlyphMap, float]:
    """Split one ASCII sample document into origin-centered glyphs.

    Args:
        document: Parsed sample sheet (one row of characters, wide spacing).
        characters: Expected characters in left-to-right order.
        cluster_threshold: Manual X-gap threshold in plotter units. When
            ``None`` the threshold is auto-calibrated to produce exactly
            ``len(characters)`` clusters.
        scale: Uniform factor applied to every glyph after centering
            (``1 / engraved text height`` normalizes to 1.0-inch design
            height).

    Returns:
        Tuple of ``(glyph_map, threshold_used)`` where ``glyph_map`` maps
        each character to its origin-centered HPGL string (empty string for
        glyphs without geometry).

    Raises:
        FontExtractionError: If the cluster count does not match the
            character count, or clustering is impossible.
    """
    paths = [p for p in document.stroke_paths if p.segments]
    if not paths:
        raise FontExtractionError("Document contains no stroke geometry")

    intervals = [Interval(path_bounds(p)[0], path_bounds(p)[2]) for p in paths]

    if cluster_threshold is None:
        threshold = auto_cluster_threshold(intervals, len(characters))
    else:
        threshold = cluster_threshold

    clusters = cluster_paths(paths, threshold)
    if len(clusters) != len(characters):
        raise FontExtractionError(
            f"Clustering produced {len(clusters)} glyph groups but the "
            f"character list has {len(characters)}. Re-check the sheet "
            f"(single row, wide spaces, Text Compose - not Frame Text "
            f"Compose) or pass --cluster-threshold (used {threshold:.3f})."
        )

    heights = sorted((c.bounds[3] - c.bounds[1]) * scale / 1000.0 for c in clusters)
    logger.info(
        "Median glyph height %.3f in after scaling (design height is 1.0 inch)",
        heights[len(heights) // 2],
    )

    glyphs: GlyphMap = {}
    for character, cluster in zip(characters, clusters):
        x_min, y_min, x_max, y_max = cluster.bounds
        dx = -(x_min + x_max) / 2.0
        dy = -(y_min + y_max) / 2.0
        moved = tuple(scale_path(translate_path(p, dx, dy), scale) for p in cluster.paths)
        glyph = emit_glyph(moved)
        if not glyph:
            logger.warning("Character %r produced no geometry", character)
        elif (x_max - x_min) * scale < 1.0 and (y_max - y_min) * scale < 1.0:
            logger.warning(
                "Character %r is degenerate (%.3f x %.3f units at design height)",
                character,
                (x_max - x_min) * scale,
                (y_max - y_min) * scale,
            )
        glyphs[character] = glyph
    return glyphs, threshold


def extract_font_file(
    plt_path: Path,
    characters: Sequence[str],
    font_name: Optional[str] = None,
    cluster_threshold: Optional[float] = None,
    text_height: Optional[float] = None,
) -> FontExtraction:
    """Extract one ``<font name> <text height>.plt`` sample sheet into a font.

    Args:
        plt_path: Path to the engraved sample PLT.
        characters: Expected characters in left-to-right order.
        font_name: Explicit font key; defaults to the file-name font part.
        cluster_threshold: Manual X-gap threshold (see
            :func:`extract_font_from_document`).
        text_height: Engraved text height in inches; overrides the value
            parsed from the file name (and lets height-less names through).

    Returns:
        The extraction result.

    Raises:
        FontExtractionError: If parsing or clustering fails, or the file
            name carries no usable text height.
    """
    if text_height is not None:
        name = font_name or plt_path.stem.title()
        height = text_height
    else:
        default_name, height = parse_font_file_name(plt_path)
        name = font_name or default_name
    if not math.isfinite(height) or height <= 0.0:
        raise FontExtractionError(
            f"{plt_path.name}: text height must be a positive finite number of inches"
        )
    scale = 1.0 / height
    parser = PLTParser()
    try:
        document = parser.parse_file(plt_path)
    except ParseError as e:
        raise FontExtractionError(f"Failed to parse {plt_path}: {e}") from e
    glyphs, threshold = extract_font_from_document(
        document, characters, cluster_threshold, scale=scale
    )
    logger.info(
        "Extracted font %r from %s: %d glyphs, threshold=%.1f units, "
        "engraved height=%.4g in, scale=%.4gx",
        name,
        plt_path.name,
        len(glyphs),
        threshold,
        height,
        scale,
    )
    return FontExtraction(font_name=name, glyphs=glyphs, threshold=threshold, text_height=height)


def load_existing_fonts(output: Path) -> Dict[str, GlyphMap]:
    """Load the existing ``plt_fonts.json`` for merging.

    Args:
        output: Path to the JSON font dictionary.

    Returns:
        Mapping of font name to glyph map; empty when the file is absent.

    Raises:
        FontExtractionError: If the file exists but is not a JSON object of
            objects with string keys and string values.
    """
    if not output.exists():
        return {}
    try:
        raw = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise FontExtractionError(f"Failed to read existing font file {output}: {e}") from e
    if not isinstance(raw, dict):
        raise FontExtractionError(f"Font file {output} must contain a JSON object")
    fonts: Dict[str, GlyphMap] = {}
    for name, glyphs in raw.items():
        if (
            not isinstance(name, str)
            or not isinstance(glyphs, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in glyphs.items())
        ):
            raise FontExtractionError(
                f"Font file {output} must map font names to objects of "
                f"single-character string keys and HPGL string values"
            )
        fonts[name] = dict(glyphs)
    return fonts


def write_fonts_json(output: Path, fonts: Dict[str, GlyphMap], characters: Sequence[str]) -> None:
    """Write the merged font dictionary deterministically.

    Font keys are sorted alphabetically; within each font the characters
    follow the ``ascii.txt`` order (any other keys are appended sorted).

    Args:
        output: Destination path (parents are created).
        fonts: Complete mapping of font name to glyph map.
        characters: Canonical character order.
    """
    ordered: Dict[str, GlyphMap] = {}
    for name in sorted(fonts):
        glyphs = fonts[name]
        merged: GlyphMap = {c: glyphs[c] for c in characters if c in glyphs}
        for key in sorted(set(glyphs) - set(merged)):
            merged[key] = glyphs[key]
        ordered[name] = merged
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
            "Extract origin-centered per-character HPGL glyphs from engraved "
            "ASCII sample sheets (Fonts/PLT-ascii/*.plt) into plt_fonts.json."
        ),
        epilog=(
            "Typical usage:\n"
            "  uv run python Fonts/extract_plt_fonts.py\n"
            "  uv run python Fonts/extract_plt_fonts.py --font-name Dino "
            "--fonts-dir Fonts/PLT-ascii -v\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--fonts-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help="Directory containing '<font name> <text height>.plt' sample "
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
        "--cluster-threshold",
        type=float,
        default=None,
        help="Manual X-gap clustering threshold in plotter units "
        "(1000 units = 1 inch). Default: auto-calibrated so the sheet "
        "splits into exactly as many glyphs as characters.",
    )
    parser.add_argument(
        "--font-name",
        default=None,
        help="Explicit font key; only valid with a single input file. "
        "Default: the file-name font part title-cased "
        "('dino 0.05.plt' -> 'Dino'). The text height still comes from "
        "the file name.",
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
        Process exit code: 0 on full success, 1 when any font failed.
    """
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s | %(message)s",
    )

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
                cluster_threshold=args.cluster_threshold,
            )
        except FontExtractionError as e:
            logger.error("%s", e)
            failed = True
            continue
        extractions.append(extraction)

    if not extractions:
        logger.error("No fonts extracted; %s left unchanged", args.output)
        return 1

    fonts = {} if args.rebuild else load_existing_fonts(args.output)
    for extraction in extractions:
        fonts[extraction.font_name] = extraction.glyphs
    try:
        write_fonts_json(args.output, fonts, characters)
    except FontExtractionError as e:
        logger.error("%s", e)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
