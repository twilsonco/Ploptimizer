#!/usr/bin/env python3
"""Per-pair kerning diagnostic for PLT-extracted fonts.

Re-walks a job spec's text lines exactly the way
:func:`plt_optimizer.generate.plt_font_renderer.render_text_line_plt_font_with_words`
lays them out, and prints the *decomposed* origin-to-origin advance of every
adjacent glyph pair::

    advance = penetration * kerning_penetration_scale * toolpath_height
            + cutter_diameter + character_spacing + kerning_min_gap

so a pair that stays wide while ``kerning_penetration_scale`` drops is
immediately explained: its penetration term is ~0 (or is manufactured by the
``min_glyph_width`` floor) and the spacing is pure additive clearance.

Columns of the report (design units = 1/1000 of the reference character):

- ``pair``  - the two characters, left then right;
- ``lw``    - the left glyph's bounding-box width;
- ``p``     - the geometric maximum silhouette penetration (no floor);
- ``p*``    - the penetration after the ``min_glyph_width`` floor, i.e. the
              value the renderer multiplies by the scale;
- ``floor`` - the ``min(left_width, min_glyph_width)`` advance floor;
- ``p*s``   - the scaled penetration (what the advance spends on closeness);
- ``adv``   - the final origin-to-origin advance in inches;
- ``gap``   - engraved stroke-to-stroke air at the tightest sampled height;
- ``bound`` - which term dominates (``penetration`` / ``floor`` / ``clearance``).

Values come from the real pipeline: the spec is parsed with the shop
``job-config.json`` and resolved against ``tools.json`` (cutter selection, the
automatic ``character_spacing = 1.5 * cutter`` fallback, toolpath height), so
the numbers match the engraved output. The ``--*-override`` flags re-resolve a
single knob without editing any file, and ``--advance-floor`` compares the
advance-floor semantics against the legacy silhouette clamp.

Usage::

    uv run python scripts/kerning_report.py "examples/job_specs/2026-09-28 adk.yaml"
    uv run python scripts/kerning_report.py <spec> --pairs "la il lt In io ic (3 ly fe ll"
    uv run python scripts/kerning_report.py <spec> --min-glyph-width-override 0
    uv run python scripts/kerning_report.py <spec> --pairs ",4" --trace -v
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow running as a plain script
    sys.path.insert(0, str(REPO_ROOT))

from plt_optimizer.generate import plt_font_renderer as kerning  # noqa: E402
from plt_optimizer.generate.font_registry import load_plt_fonts, resolve_font  # noqa: E402
from plt_optimizer.generate.plt_font_renderer import (  # noqa: E402
    _glyph_entry,
    _GlyphGeometry,
    interpolate_envelope,
)
from plt_optimizer.generate.resolution import (  # noqa: E402
    ResolvedLabel,
    ResolvedTextLine,
    resolve_job_spec,
)
from plt_optimizer.generate.schema import JobSpec, parse_yaml  # noqa: E402
from plt_optimizer.generate.substitution import expand_job_spec  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_TOOLS_PATH: Path = REPO_ROOT / "tools.json"
DEFAULT_JOB_CONFIG_PATH: Path = REPO_ROOT / "job-config.json"
DEFAULT_FONTS_PATH: Path = REPO_ROOT / "Fonts" / "plt_fonts.json"

# Design units per rendered inch (the PLT storage frame).
_UNITS_PER_INCH: float = 1000.0

# Default recession multiplier, read from the resolution module when the
# cascade already carries the field and falling back to the geometric 1.0.
# ``getattr`` keeps the diagnostic working on both sides of the change.
_RECESSION_DEFAULT: float = float(getattr(kerning, "KERNING_RECESSION_SCALE", 1.0))


@dataclass(frozen=True)
class KerningOverrides:
    """Optional single-knob overrides applied to every resolved line.

    A ``None`` field keeps the value resolved from the spec/config cascade.

    Attributes:
        penetration_scale: Override for ``kerning_penetration_scale``.
        recession_scale: Override for ``kerning_recession_scale``.
        min_glyph_width: Override for ``min_glyph_width`` (inches).
        window_fraction: Override for ``kerning_window_fraction``.
        kerning_min_gap: Override for ``kerning_min_gap`` (inches).
        fallback_fraction: Override for ``fallback_advance_fraction``.
    """

    penetration_scale: Optional[float] = None
    recession_scale: Optional[float] = None
    min_glyph_width: Optional[float] = None
    window_fraction: Optional[float] = None
    kerning_min_gap: Optional[float] = None
    fallback_fraction: Optional[float] = None


@dataclass(frozen=True)
class LineFrame:
    """The resolved per-line numbers a kerning advance is built from.

    Attributes:
        scale: Output inches per design unit (``toolpath_height / ref_height``).
        toolpath_height: Rendered reference-character height in inches.
        clearance: ``cutter + character_spacing + kerning_min_gap`` (inches).
        cutter_diameter: Resolved text cutter diameter in inches.
        character_spacing: Resolved extra character spacing in inches.
        kerning_min_gap: Resolved extra kerning gap in inches.
        min_width_design: ``min_glyph_width`` converted to design units.
        window_design: Kerning half-window in design units.
        penetration_scale: Resolved penetration multiplier.
        recession_scale: Resolved recession (negative penetration) multiplier.
        fallback_fraction: Resolved bbox-fallback multiplier.
        font: Canonical PLT font name.
    """

    scale: float
    toolpath_height: float
    clearance: float
    cutter_diameter: float
    character_spacing: float
    kerning_min_gap: float
    min_width_design: float
    window_design: float
    penetration_scale: float
    recession_scale: float
    fallback_fraction: float
    font: str


@dataclass(frozen=True)
class SampleTrace:
    """One sampled height of a pair's silhouette comparison.

    Attributes:
        y: Sampled height (design units).
        left_sil: Left glyph's right silhouette x at ``y`` (design units).
        right_sil: Right glyph's left silhouette x at ``y`` (design units).
        deepest_right: Deepest (minimum) right-glyph left silhouette within
            the kerning window of ``y``.
        penetration: ``max(left_sil, floor) - deepest_right`` (design units).
    """

    y: float
    left_sil: float
    right_sil: float
    deepest_right: float
    penetration: float


@dataclass(frozen=True)
class PairMetrics:
    """Fully decomposed kerning advance for one adjacent glyph pair.

    Attributes:
        left: Left character.
        right: Right character.
        line_index: Index of the text line the pair was read from.
        position: Index of the left character inside that line.
        left_width: Left glyph bbox width (design units).
        right_width: Right glyph bbox width (design units).
        penetration: Geometric maximum penetration, no floor (design units).
        effective: Penetration after the ``min_glyph_width`` floor - the value
            the renderer multiplies by the scale (design units).
        floor: The ``min(left_width, min_glyph_width)`` advance floor
            (design units).
        scaled: ``effective`` after the penetration/recession multipliers
            (design units).
        advance: Final origin-to-origin advance in inches.
        gap: Engraved stroke-to-stroke air at the tightest sampled height, in
            inches (the advance minus the unscaled geometric closeness).
        bound_by: Which term dominates the advance.
        fallback: True when the pair used the bbox-width fallback (no
            overlapping height, or a missing envelope).
        samples: Per-height silhouette trace (populated only with ``trace``).
    """

    left: str
    right: str
    line_index: int
    position: int
    left_width: float
    right_width: float
    penetration: float
    effective: float
    floor: float
    scaled: float
    advance: float
    gap: float
    bound_by: str
    fallback: bool
    samples: Tuple[SampleTrace, ...] = ()

    @property
    def pair(self) -> str:
        """The two characters as a printable string."""
        return f"{self.left}{self.right}"


def load_inventory(tools_path: Path) -> Tuple[Optional[List[float]], Optional[float]]:
    """Load ``available_cutters`` / ``boundary_hole_cutter_size`` from tools.json.

    Mirrors ``plt_optimizer/cli/generate.py::_load_cutter_inventory``.

    Args:
        tools_path: Path to ``tools.json``.

    Returns:
        ``(available_cutters, boundary_hole_cutter_size)``; both ``None`` when
        the file is absent (ideal cutters are used then).
    """
    if not tools_path.is_file():
        logger.warning("tools file not found: %s (ideal cutters used)", tools_path)
        return None, None
    with open(tools_path, encoding="utf-8") as handle:
        data = json.load(handle)
    return (data.get("available_cutters") or None), data.get("boundary_hole_cutter_size")


def load_job(job_yaml: Path, job_config_path: Optional[Path]) -> JobSpec:
    """Parse + expand a job spec with the shop job config injected.

    Args:
        job_yaml: Path to the job spec YAML.
        job_config_path: Path to ``job-config.json`` (skipped when absent).

    Returns:
        The parsed and replacement-expanded :class:`JobSpec`.
    """
    config_path = job_config_path if job_config_path and job_config_path.is_file() else None
    if config_path is None and job_config_path is not None:
        logger.warning("job config not found: %s", job_config_path)
    job = parse_yaml(job_yaml, job_config_path=config_path)
    return expand_job_spec(job, job_yaml)


def _pick(override: Optional[float], resolved: float) -> float:
    """Return the override when set, else the resolved cascade value.

    Args:
        override: The command-line override (``None`` = unset).
        resolved: The value resolved from the spec/config cascade.

    Returns:
        The effective value.
    """
    return resolved if override is None else override


def scale_penetration(penetration: float, scale: float, recession_scale: float) -> float:
    """Apply the penetration/recession multipliers to one penetration value.

    Positive penetrations (the glyphs overlap) use ``scale``; negative ones
    (the silhouettes are recessed from each other) use ``recession_scale``, so
    tightening the tight pairs no longer pulls the gapped pairs closer.

    Args:
        penetration: The effective (floored) penetration in design units.
        scale: ``kerning_penetration_scale``.
        recession_scale: ``kerning_recession_scale``.

    Returns:
        The scaled penetration in design units.
    """
    if penetration >= 0.0:
        return penetration * scale
    return penetration * recession_scale


def build_frame(
    line: ResolvedTextLine,
    overrides: KerningOverrides,
    json_path: Path,
) -> LineFrame:
    """Compute the resolved kerning frame for one text line.

    Mirrors the setup in
    :func:`plt_optimizer.generate.plt_font_renderer.render_text_line_plt_font_with_words`.

    Args:
        line: The resolved text line.
        overrides: Single-knob overrides applied on top of the cascade.
        json_path: ``plt_fonts.json`` location (the glyph cache key).

    Returns:
        The line's :class:`LineFrame`.

    Raises:
        ValueError: If the line's font is not a PLT-extracted font (this
            diagnostic only decomposes PLT envelope kerning).
    """
    ref = resolve_font(line.font, json_path=json_path)
    if ref.kind != "plt":
        raise ValueError(f"font {ref.name!r} is a TrueType font; no envelope kerning to report")

    font_data = load_plt_fonts(json_path)[ref.name]
    scale = line.toolpath_text_height / font_data.normalized_ref_height

    min_glyph_width = _pick(overrides.min_glyph_width, line.min_glyph_width)
    kerning_min_gap = _pick(overrides.kerning_min_gap, line.kerning_min_gap)

    return LineFrame(
        scale=scale,
        toolpath_height=line.toolpath_text_height,
        clearance=line.cutter_diameter + line.character_spacing + kerning_min_gap,
        cutter_diameter=line.cutter_diameter,
        character_spacing=line.character_spacing,
        kerning_min_gap=kerning_min_gap,
        min_width_design=min_glyph_width / scale if scale > 0 else 0.0,
        window_design=0.5
        * _pick(overrides.window_fraction, line.kerning_window_fraction)
        * font_data.normalized_ref_height,
        penetration_scale=_pick(overrides.penetration_scale, line.kerning_penetration_scale),
        recession_scale=_pick(
            overrides.recession_scale,
            getattr(line, "kerning_recession_scale", _RECESSION_DEFAULT),
        ),
        fallback_fraction=_pick(overrides.fallback_fraction, line.fallback_advance_fraction),
        font=ref.name,
    )


def pair_metrics(
    left: _GlyphGeometry,
    right: _GlyphGeometry,
    frame: LineFrame,
    left_char: str,
    right_char: str,
    *,
    line_index: int = -1,
    position: int = -1,
    legacy_floor: bool = True,
    trace: bool = False,
) -> PairMetrics:
    """Decompose the kerning advance of one adjacent glyph pair.

    Reimplements :func:`plt_optimizer.generate.plt_font_renderer.kerning_offset`
    while keeping every intermediate term, so the report can attribute the
    advance to penetration, the ``min_glyph_width`` floor, or clearance.

    Args:
        left: The earlier (left) glyph geometry.
        right: The following (right) glyph geometry.
        frame: The resolved per-line kerning frame.
        left_char: Left character (reporting only).
        right_char: Right character (reporting only).
        line_index: Text line index (reporting only).
        position: Index of the left character in that line (reporting only).
        legacy_floor: When True, apply ``min_glyph_width`` as a per-sample
            clamp of the left glyph's right silhouette (the semantics in place
            before the advance-floor fix). When False, apply it as a floor on
            the advance, capped by the left glyph's own width.
        trace: Also record the per-sample silhouette comparison.

    Returns:
        The pair's :class:`PairMetrics`.
    """
    left_box = left.bounding_box
    right_box = right.bounding_box
    left_width = left_box[2] - left_box[0]
    right_width = right_box[2] - right_box[0]
    floor = min(left_width, frame.min_width_design)

    y_low = max(left_box[1], right_box[1])
    y_high = min(left_box[3], right_box[3])
    envelope_ok = y_high >= y_low and bool(left.right_envelope) and bool(right.left_envelope)

    if not envelope_ok:
        fallback = max(left_width, frame.min_width_design) * frame.fallback_fraction
        return PairMetrics(
            left=left_char,
            right=right_char,
            line_index=line_index,
            position=position,
            left_width=left_width,
            right_width=right_width,
            penetration=fallback,
            effective=fallback,
            floor=floor,
            scaled=fallback,
            advance=fallback * frame.scale + frame.clearance,
            gap=frame.clearance,
            bound_by="fallback",
            fallback=True,
        )

    s_low = max(y_low - frame.window_design, min(left_box[1], right_box[1]))
    s_high = min(y_high + frame.window_design, max(left_box[3], right_box[3]))

    ys: List[float] = []
    left_sils: List[float] = []
    right_sils: List[float] = []
    for y in kerning._kerning_sample_heights(
        left.right_envelope, right.left_envelope, s_low, s_high
    ):
        left_sil = interpolate_envelope(left.right_envelope, y)
        right_sil = interpolate_envelope(right.left_envelope, y)
        if left_sil is None or right_sil is None:  # pragma: no cover - guards
            continue
        ys.append(y)
        left_sils.append(left_sil)
        right_sils.append(right_sil)

    samples: List[SampleTrace] = []
    geometric: List[float] = []
    windowed: List[float] = []
    for k, (y_k, left_sil) in enumerate(zip(ys, left_sils)):
        lo = 0
        while ys[lo] < y_k - frame.window_design:
            lo += 1
        hi = k
        while hi + 1 < len(ys) and ys[hi + 1] <= y_k + frame.window_design:
            hi += 1
        deepest = min(right_sils[lo : hi + 1])
        geometric.append(left_sil - deepest)
        floored_left_sil = max(left_sil, frame.min_width_design) if legacy_floor else left_sil
        penetration = floored_left_sil - deepest
        windowed.append(penetration)
        if trace:
            samples.append(
                SampleTrace(
                    y=y_k,
                    left_sil=left_sil,
                    right_sil=right_sils[k],
                    deepest_right=deepest,
                    penetration=penetration,
                )
            )

    penetration = max(geometric)
    effective = max(windowed)
    scaled = scale_penetration(effective, frame.penetration_scale, frame.recession_scale)
    advance_design = scaled if legacy_floor else max(scaled, floor)
    advance = advance_design * frame.scale + frame.clearance
    # Real engraved stroke-to-stroke air at the tightest sampled height: the
    # advance minus the *unscaled* geometric closeness it removes.
    gap = advance - penetration * frame.scale

    if effective > penetration + 1e-9:
        bound_by = "floor"
    elif abs(advance_design) < 1e-12:
        bound_by = "clearance"
    else:
        bound_by = "penetration"

    return PairMetrics(
        left=left_char,
        right=right_char,
        line_index=line_index,
        position=position,
        left_width=left_width,
        right_width=right_width,
        penetration=penetration,
        effective=effective,
        floor=floor,
        scaled=scaled,
        advance=advance,
        gap=gap,
        bound_by=bound_by,
        fallback=False,
        samples=tuple(samples),
    )


def report_lines(
    labels: Sequence[ResolvedLabel],
    overrides: KerningOverrides,
    json_path: Path,
    *,
    legacy_floor: bool,
    trace: bool,
    max_rows: Optional[int],
) -> Tuple[List[PairMetrics], List[str]]:
    """Compute the metrics of every adjacent pair of every resolved label.

    Args:
        labels: Resolved labels to walk.
        overrides: Single-knob overrides.
        json_path: ``plt_fonts.json`` location.
        legacy_floor: Use the silhouette-clamp floor semantics.
        trace: Record per-sample traces.
        max_rows: Stop after this many distinct pairs (``None`` = all).

    Returns:
        ``(metrics, headers)`` where ``metrics`` holds one entry per distinct
        adjacent pair (the tightest advance wins) and ``headers`` the
        per-line resolved-number banner lines.
    """
    metrics: dict[Tuple[str, str], PairMetrics] = {}
    headers: List[str] = []
    seen_frames: set[Tuple[str, float, float]] = set()

    for label in labels:
        for line_index, line in enumerate(label.content):
            try:
                frame = build_frame(line, overrides, json_path)
            except ValueError as exc:
                logger.warning("skipping line %d of %s: %s", line_index, label.id, exc)
                continue

            key = (frame.font, line.toolpath_text_height, frame.clearance)
            if key not in seen_frames:
                seen_frames.add(key)
                headers.append(
                    f"line {line_index} {line.text!r}: font={frame.font} "
                    f"nominal={line.nominal_text_height:.4f}in "
                    f"cutter={frame.cutter_diameter:.4f}in "
                    f"toolpath={frame.toolpath_height:.4f}in "
                    f"char_spacing={frame.character_spacing:.4f}in "
                    f"min_gap={frame.kerning_min_gap:.4f}in "
                    f"clearance={frame.clearance:.4f}in "
                    f"min_glyph_width={frame.min_width_design * _UNITS_PER_INCH:.1f}u "
                    f"window={frame.window_design * _UNITS_PER_INCH:.1f}u "
                    f"scale={frame.penetration_scale:g} "
                    f"recession={frame.recession_scale:g}"
                )

            word_start = 0
            for word in line.text.split(" "):
                for offset in range(len(word) - 1):
                    left_char, right_char = word[offset], word[offset + 1]
                    metric = pair_metrics(
                        _glyph_entry(str(json_path), frame.font, left_char),
                        _glyph_entry(str(json_path), frame.font, right_char),
                        frame,
                        left_char,
                        right_char,
                        line_index=line_index,
                        position=word_start + offset,
                        legacy_floor=legacy_floor,
                        trace=trace,
                    )
                    best = metrics.get((left_char, right_char))
                    if best is None or metric.advance < best.advance:
                        metrics[(left_char, right_char)] = metric
                    if max_rows is not None and len(metrics) >= max_rows:
                        return list(metrics.values()), headers
                word_start += len(word) + 1

    return list(metrics.values()), headers


def format_table(metrics: Sequence[PairMetrics]) -> str:
    """Render the metrics as a fixed-width table, widest advance first.

    Args:
        metrics: Pairs to print (any order).

    Returns:
        The formatted table.
    """
    header = (
        f"{'pair':>7}  {'lw(u)':>7}  {'rw(u)':>7}  {'p(u)':>8}  {'p*(u)':>8}  "
        f"{'floor(u)':>8}  {'p*s(u)':>8}  {'adv(in)':>8}  {'gap(in)':>8}  bound"
    )
    lines = [header, "-" * len(header)]
    for metric in sorted(metrics, key=lambda m: m.advance, reverse=True):
        lines.append(
            f"{metric.pair!r:>7}  {metric.left_width * _UNITS_PER_INCH:7.1f}  "
            f"{metric.right_width * _UNITS_PER_INCH:7.1f}  "
            f"{metric.penetration * _UNITS_PER_INCH:8.1f}  "
            f"{metric.effective * _UNITS_PER_INCH:8.1f}  "
            f"{metric.floor * _UNITS_PER_INCH:8.1f}  "
            f"{metric.scaled * _UNITS_PER_INCH:8.1f}  "
            f"{metric.advance:8.4f}  {metric.gap:8.4f}  {metric.bound_by}"
        )
    return "\n".join(lines)


def filter_pairs(metrics: Sequence[PairMetrics], pairs: Sequence[str]) -> List[PairMetrics]:
    """Select the metrics matching one of the requested pair strings.

    Args:
        metrics: All computed pairs.
        pairs: Requested 2-character pairs (matched in either character order).

    Returns:
        The matching metrics, in the requested order.
    """
    by_pair = {(metric.left, metric.right): metric for metric in metrics}
    selected: List[PairMetrics] = []
    for requested in pairs:
        if len(requested) != 2:
            logger.warning("ignoring --pairs entry %r (needs exactly 2 characters)", requested)
            continue
        left, right = requested[0], requested[1]
        metric = by_pair.get((left, right)) or by_pair.get((right, left))
        if metric is None:
            logger.warning("pair %r not found in the spec's text lines", requested)
            continue
        selected.append(metric)
    return selected


def format_trace(metric: PairMetrics) -> str:
    """Render a pair's per-sample silhouette trace.

    Args:
        metric: The pair to trace.

    Returns:
        One line per sampled height (design units).
    """
    lines = [
        f"trace {metric.pair!r}: R = left glyph's right silhouette, "
        f"L = right glyph's left silhouette, Lmin = deepest L in window, "
        f"p = R - Lmin (design units)"
    ]
    for sample in metric.samples:
        lines.append(
            f"  y={sample.y * _UNITS_PER_INCH:9.2f}  R={sample.left_sil * _UNITS_PER_INCH:8.2f}  "
            f"L={sample.right_sil * _UNITS_PER_INCH:8.2f}  "
            f"Lmin={sample.deepest_right * _UNITS_PER_INCH:8.2f}  "
            f"p={sample.penetration * _UNITS_PER_INCH:8.2f}"
        )
    return "\n".join(lines)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(
        description="Decompose PLT-font envelope kerning advances per glyph pair.",
    )
    parser.add_argument("spec", type=Path, help="Job spec YAML to analyse")
    parser.add_argument(
        "--tools",
        type=Path,
        default=DEFAULT_TOOLS_PATH,
        help=f"tools.json path (default: {DEFAULT_TOOLS_PATH.name})",
    )
    parser.add_argument(
        "--fonts",
        type=Path,
        default=DEFAULT_FONTS_PATH,
        help="plt_fonts.json path",
    )
    parser.add_argument(
        "--job-config",
        type=Path,
        default=DEFAULT_JOB_CONFIG_PATH,
        help=f"job-config.json path (default: {DEFAULT_JOB_CONFIG_PATH.name})",
    )
    parser.add_argument(
        "--pairs",
        default=None,
        help="Whitespace-separated 2-character pairs to report (default: every pair)",
    )
    parser.add_argument(
        "--scale-override",
        type=float,
        default=None,
        help="Override kerning_penetration_scale",
    )
    parser.add_argument(
        "--recession-override",
        type=float,
        default=None,
        help="Override kerning_recession_scale",
    )
    parser.add_argument(
        "--min-glyph-width-override",
        type=float,
        default=None,
        help="Override min_glyph_width (inches)",
    )
    parser.add_argument(
        "--window-override",
        type=float,
        default=None,
        help="Override kerning_window_fraction",
    )
    parser.add_argument(
        "--min-gap-override",
        type=float,
        default=None,
        help="Override kerning_min_gap (inches)",
    )
    parser.add_argument(
        "--advance-floor",
        action="store_true",
        help="Report the advance-floor min_glyph_width semantics instead of the "
        "legacy silhouette clamp",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Print the per-sample silhouette comparison for reported pairs",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of distinct pairs computed (default: all)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the report.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 on success, 2 on a missing spec, 1 when no PLT
        kerning pair was found).
    """
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    spec_path: Path = args.spec
    if not spec_path.is_file():
        print(f"spec not found: {spec_path}", file=sys.stderr)
        return 2

    inventory, boundary_cutter = load_inventory(args.tools)
    job = load_job(spec_path, args.job_config)
    labels = resolve_job_spec(
        job,
        available_cutters=inventory,
        boundary_hole_cutter_size=boundary_cutter,
    )

    overrides = KerningOverrides(
        penetration_scale=args.scale_override,
        recession_scale=args.recession_override,
        min_glyph_width=args.min_glyph_width_override,
        window_fraction=args.window_override,
        kerning_min_gap=args.min_gap_override,
    )
    legacy_floor = not args.advance_floor

    metrics, headers = report_lines(
        labels,
        overrides,
        args.fonts,
        legacy_floor=legacy_floor,
        trace=args.trace,
        max_rows=args.limit,
    )
    if not metrics:
        print("no PLT-font kerning pairs found in this spec", file=sys.stderr)
        return 1

    print(f"spec: {spec_path}")
    print(f"floor semantics: {'legacy silhouette clamp' if legacy_floor else 'advance floor'}")
    for header in headers:
        print(f"  {header}")
    print()

    selected = filter_pairs(metrics, args.pairs.split()) if args.pairs else metrics
    print(format_table(selected))

    if args.trace:
        print()
        for metric in selected:
            print(format_trace(metric))

    return 0


if __name__ == "__main__":
    sys.exit(main())
