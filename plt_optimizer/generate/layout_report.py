"""Shared formatting for the per-label LAYOUT REPORT (compression + spacing).

The report surfaces the two render-time typography effects that change
what is engraved without appearing in the YAML spec:

- **Horizontal compression** (:attr:`RenderedLabel.compression_by_line`):
  the effective per-line scale (collision-avoidance scale x margin-overflow
  scale) applied while rendering.
- **Cutter downsizing**
  (:attr:`plt_optimizer.generate.resolution.ResolvedLabel.cutter_downsize_by_line`
  on the rendered label's ``source_label``): the automatic cutter a
  compressed line was reduced to, by the compression-driven cutter reduction
  pre-pass.
- **Vertical line spacing** (:attr:`RenderedLabel.line_spacing_by_line`):
  the effective gap *below* each line, after
  :func:`plt_optimizer.generate.resolution.fit_line_spacing_to_margins`
  clamped it at render time to preserve the vertical margins. The
  requested value is read from the rendered label's
  ``source_label.content[i].line_spacing`` (the adjusted clone, so
  collision-phase clones report correctly), so the report shows both the
  requested and the effective spacing whenever they differ.
- **Descending-character relief**
  (:attr:`RenderedLabel.descender_adjustments_by_line`): the individual
  descending glyphs the automatic descending-character adjustment vertically
  downscaled and raised because a glyph on the following line came close to
  colliding with them (``auto_descending_character_adjustment``).
- **Bold markup** (:attr:`plt_optimizer.generate.resolution.ResolvedTextLine.bold_cutter_diameter`):
  the thicker cutter a ``<b>`` run of the line engraves with, alongside the
  line's own (plain) cutter -- the tool swap the markup caused, which the YAML
  spec never states.
- **Scale markup** (:attr:`plt_optimizer.generate.resolution.ResolvedTextLine.scale_values`):
  the ``<scale:N>`` factor range a line's runs engrave at, alongside the
  cutter the line stepped down to when a run needed a smaller tool -- the
  geometry change the markup caused, which the YAML spec never states.
- **Superscript/subscript markup**
  (:attr:`plt_optimizer.generate.resolution.ResolvedTextLine.sup_sub_shifts`):
  the baseline-shift range a line's ``<sup>``/``<sub>`` runs engrave at (in
  cap-height units), alongside the cutter the downscaled runs stepped to --
  the geometry change the markup caused, which the YAML spec never states.
- **Font markup**
  (:attr:`plt_optimizer.generate.resolution.ResolvedTextLine.font_runs`):
  the fonts a line's ``<f:...>`` runs engrave with, alongside the cutters
  those runs stepped to (each run font's own ladder) -- the font/tool swap
  the markup caused, which the YAML spec never states.

This module is the single source of truth for the report text: the
``generate`` CLI (compact by default, full under ``-v``) and
``scripts/run_integration_test.py`` (full, Phase 3.6) both print the
lines returned here, so the two surfaces can never diverge.

The module is deliberately import-light (pure Python; pipeline types are
``TYPE_CHECKING``-only imports) so it stays unit-testable without
matplotlib and safe on the CLI's lazy-import path.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Mapping, Sequence

if TYPE_CHECKING:
    from plt_optimizer.generate.label_renderer import RenderedLabel
    from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine

# Float tolerance for requested-vs-effective spacing comparisons. Values
# within this band count as "no adjustment" (never printed as a deviation
# and never a compact-mode finding). AGENTS.md forbids ``==`` on floats.
_SPACING_TOLERANCE: float = 1e-9

# Summary line for a label printed in full mode that has neither a
# compressed line nor any spacing gap (single-line, untouched labels).
_UNTOUCHED_LINE = "    all lines at natural width / spacing"

# Footer for a job whose report contains no findings at all.
_NO_FINDINGS_FOOTER = (
    "\u2713 No compression or line-spacing adjustments applied anywhere in this job."
)


def _spacing_deviation(requested: float, effective: float) -> bool:
    """Return whether an effective gap deviates from the requested spacing.

    Args:
        requested: The spacing requested on the resolved text line (inches).
        effective: The render-time, margin-clamped gap actually used (inches).

    Returns:
        ``True`` when the values differ beyond :data:`_SPACING_TOLERANCE`.
    """
    return not math.isclose(
        requested, effective, rel_tol=_SPACING_TOLERANCE, abs_tol=_SPACING_TOLERANCE
    )


def _scale_run_cutters(line: ResolvedTextLine) -> tuple[float, ...]:
    """Return the distinct cutters a line's ``<scale:N>``/``<sup>``/``<sub>`` runs use.

    Ascending, de-duplicated, read from the derived per-character cutters.
    Falls back to the line's own cutter when no character scales.
    """
    scales = line.scale_values
    sup_scales = line.sup_sub_scales
    cutters = line.per_char_cutters
    used = {
        cutter
        for index, cutter in enumerate(cutters)
        if (index < len(scales) and scales[index] != 1.0)
        or (index < len(sup_scales) and sup_scales[index] != 1.0)
    }
    return tuple(sorted(used)) if used else (line.cutter_diameter,)


def _font_run_info(line: ResolvedTextLine) -> tuple[tuple[str, ...], tuple[float, ...]]:
    """Return the ``(fonts, cutters)`` a line's ``<f:...>`` runs engrave with.

    Both ascending and de-duplicated, read from the derived per-character
    arrays: the run fonts (the line's own font is ``None`` and never named --
    the finding exists to surface the fonts the tag introduced) and the
    cutters those characters step to (the run font's own ladder, so a symbol
    run's thin tool is visible next to the line's fat one).
    """
    fonts = line.font_runs
    cutters = line.per_char_cutters
    names: set[str] = set()
    used: set[float] = set()
    for index, name in enumerate(fonts):
        if name is None:
            continue
        names.add(name)
        used.add(cutters[index] if index < len(cutters) else line.cutter_diameter)
    return tuple(sorted(names)), tuple(sorted(used))


def _label_findings(
    rendered: RenderedLabel,
) -> list[
    tuple[
        int,
        str,
        float | None,
        float | None,
        float | None,
        tuple[float, float] | None,
        int,
        tuple[float, float] | None,
        tuple[float, float] | None,
        tuple[float, float] | None,
        tuple[tuple[str, ...], tuple[float, ...]] | None,
    ]
]:
    """Collect the per-line findings (compression / spacing / downsize / relief / bold / scale / sup-sub / font).

    Args:
        rendered: The rendered label carrying the render-time maps plus the
            (possibly adjusted) source label with the requested spacing and
            any compression-driven cutter downsizes.

    Returns:
        One ``(line_index, line_text, scale, requested, effective, downsize,
        relief_count, bold, markup_scale, sup_sub, font_runs)`` tuple per
        finding, in content order. ``scale`` is set (below ``1.0``) only for
        compressed lines; ``requested``/``effective`` are set only for spacing
        gaps (always in full mode, or when deviating in compact mode);
        ``downsize`` is the ``(original, final)`` cutter pair only for lines
        whose automatic cutter was reduced; ``relief_count`` is how many
        descending glyphs the automatic descending-character adjustment
        relieved on that line (``0`` for none); ``bold`` is the ``(plain,
        bold)`` cutter pair only for lines carrying a ``<b>`` run;
        ``markup_scale`` is the ``(min, max)`` ``<scale:N>`` factor range
        only for lines carrying a ``<scale:N>`` run; ``sup_sub`` is the
        ``(min, max)`` baseline-shift range (cap-height units) only for lines
        carrying a ``<sup>``/``<sub>`` run; ``font_runs`` is the ``(fonts,
        cutters)`` pair only for lines carrying a ``<f:...>`` run.
    """
    content = rendered.source_label.content
    downsizes = rendered.source_label.cutter_downsize_by_line
    reliefs = rendered.descender_adjustments_by_line
    findings: list[
        tuple[
            int,
            str,
            float | None,
            float | None,
            float | None,
            tuple[float, float] | None,
            int,
            tuple[float, float] | None,
            tuple[float, float] | None,
            tuple[float, float] | None,
            tuple[tuple[str, ...], tuple[float, ...]] | None,
        ]
    ] = []
    for line_index in sorted(
        set(rendered.compression_by_line)
        | set(rendered.line_spacing_by_line)
        | set(downsizes)
        | set(reliefs)
        | {index for index, line in enumerate(content) if line.bold_cutter_diameter is not None}
        | {index for index, line in enumerate(content) if line.has_scale}
        | {index for index, line in enumerate(content) if line.has_sup_sub}
        | {index for index, line in enumerate(content) if line.has_font_runs}
    ):
        if line_index >= len(content):  # pragma: no cover - defensive
            continue
        line = content[line_index]
        scale = rendered.compression_by_line.get(line_index)
        compressed = scale is not None and scale < 1.0
        effective = rendered.line_spacing_by_line.get(line_index)
        requested = float(line.line_spacing)
        deviates = effective is not None and _spacing_deviation(requested, effective)
        downsize = downsizes.get(line_index)
        relief_count = len(reliefs.get(line_index, ()))
        bold = (
            (line.cutter_diameter, line.bold_cutter_diameter)
            if line.bold_cutter_diameter is not None
            else None
        )
        markup_scale: tuple[float, float] | None = None
        if line.has_scale:
            values = line.scale_values
            markup_scale = (min(values), max(values))
        sup_sub: tuple[float, float] | None = None
        if line.has_sup_sub:
            shifts = line.sup_sub_shifts
            sup_sub = (min(shifts), max(shifts))
        font_runs: tuple[tuple[str, ...], tuple[float, ...]] | None = None
        if line.has_font_runs:
            font_runs = _font_run_info(line)
        if (
            not compressed
            and not deviates
            and downsize is None
            and relief_count == 0
            and bold is None
            and markup_scale is None
            and sup_sub is None
            and font_runs is None
        ):
            continue
        findings.append(
            (
                line_index,
                line.plain_text,
                scale if compressed else None,
                float(requested) if deviates else None,
                effective if deviates else None,
                downsize,
                relief_count,
                bold,
                markup_scale,
                sup_sub,
                font_runs,
            )
        )
    return findings


def _label_lines(
    rendered: RenderedLabel,
    *,
    full: bool,
) -> list[str]:
    """Format the report lines for one label (header included).

    Args:
        rendered: The rendered label to report.
        full: When True, every spacing gap is printed (even when effective
            equals the requested value) and untouched labels get a summary
            line. When False, only findings (compressed lines, deviating
            gaps) are printed and untouched labels produce no lines.

    Returns:
        The report lines for the label; empty when a compact-mode label
        has no findings.
    """
    content = rendered.source_label.content
    findings = _label_findings(rendered)
    finding_by_index: dict[
        int,
        tuple[
            float | None,
            float | None,
            float | None,
            tuple[float, float] | None,
            int,
            tuple[float, float] | None,
            tuple[float, float] | None,
            tuple[float, float] | None,
            tuple[tuple[str, ...], tuple[float, ...]] | None,
        ],
    ] = {
        index: (
            scale,
            requested,
            effective,
            downsize,
            relief_count,
            bold,
            markup_scale,
            sup_sub,
            font_runs,
        )
        for index, _t, scale, requested, effective, downsize, relief_count, bold, markup_scale, sup_sub, font_runs in findings
    }

    if not full and not findings:
        return []

    lines = [f"Label {rendered.source_label.id}:"]

    # Add margin summary line
    h_margin = rendered.source_label.h_margin
    v_margin = rendered.source_label.v_margin
    lines.append(
        f"    Margins: horizontal {h_margin:.3f}in (left/right), "
        f"vertical {v_margin:.3f}in (top/bottom)"
    )

    # Add spacing summary section showing all line gaps
    if rendered.line_spacing_by_line or content:
        spacing_lines: list[str] = []
        for line_index in range(len(content) - 1):  # n-1 gaps for n lines
            effective = rendered.line_spacing_by_line.get(line_index)
            if effective is not None:
                requested: float = content[line_index].line_spacing
                if _spacing_deviation(requested, effective):
                    spacing_lines.append(
                        f"      Line {line_index}: {effective:.3f}in (requested {requested:.3f}in)"
                    )
                else:
                    spacing_lines.append(f"      Line {line_index}: {effective:.3f}in")

        if spacing_lines:
            lines.append("    Line spacing (gaps between lines):")
            lines.extend(spacing_lines)

    if full and not findings and not rendered.line_spacing_by_line:
        lines.append(_UNTOUCHED_LINE)
        return lines

    for line_index, line in enumerate(content):
        entry = finding_by_index.get(line_index)
        if entry is not None:
            scale = entry[0]
            requested = entry[1]  # type: ignore[assignment]
            effective = entry[2]
            downsize = entry[3]
            if scale is not None:
                lines.append(
                    f"    Line {line_index}: {line.plain_text!r} "
                    f"scale {scale:.3f} ({(1.0 - scale) * 100.0:.1f}% compressed)"
                )
            if downsize is not None:
                original, final = downsize
                lines.append(
                    f"    Line {line_index}: {line.plain_text!r} "
                    f"cutter {original:.3f}in -> {final:.3f}in (downsized for compression)"
                )
            bold = entry[5]
            if bold is not None:
                plain_cutter, bold_cutter = bold
                lines.append(
                    f"    Line {line_index}: {line.plain_text!r} "
                    f"bold cutter {bold_cutter:.3f}in (line cutter {plain_cutter:.3f}in)"
                )
            markup_scale = entry[6]
            if markup_scale is not None:
                low, high = markup_scale
                factor_text = f"{low:.3f}" if low == high else f"{low:.3f}-{high:.3f}"
                # Each scaled run engraves with the cutter for its effective
                # height, so name the run cutters (the tool split the markup
                # caused), not just the line's own tool.
                run_cutters = _scale_run_cutters(line)
                cutter_text = (
                    f"{run_cutters[0]:.3f}in"
                    if len(run_cutters) == 1
                    else "-".join(f"{cutter:.3f}" for cutter in run_cutters) + "in"
                )
                lines.append(
                    f"    Line {line_index}: {line.plain_text!r} "
                    f"scaled {factor_text} (run cutter {cutter_text}, "
                    f"line cutter {line.cutter_diameter:.3f}in)"
                )
            sup_sub = entry[7]
            if sup_sub is not None:
                low, high = sup_sub
                shift_text = f"{low:.3f}" if low == high else f"{low:.3f}-{high:.3f}"
                # A bare <sup>/<sub> also downscales, so its run engraves with
                # the cutter for the downscaled height (the <scale:N> ladder);
                # the argumented form shifts only and keeps the line's tool.
                run_cutters = _scale_run_cutters(line)
                cutter_text = (
                    f"{run_cutters[0]:.3f}in"
                    if len(run_cutters) == 1
                    else "-".join(f"{cutter:.3f}" for cutter in run_cutters) + "in"
                )
                lines.append(
                    f"    Line {line_index}: {line.plain_text!r} "
                    f"sup/sub shift {shift_text} cap (run cutter {cutter_text}, "
                    f"line cutter {line.cutter_diameter:.3f}in)"
                )
            font_runs = entry[8]
            if font_runs is not None:
                names, run_cutters = font_runs
                # The run fonts are invisible in the YAML (they ride the tag)
                # and each run steps to its own font's ladder cutter, so name
                # both the fonts and the tools the markup caused.
                cutter_text = (
                    f"{run_cutters[0]:.3f}in"
                    if len(run_cutters) == 1
                    else "-".join(f"{cutter:.3f}" for cutter in run_cutters) + "in"
                )
                lines.append(
                    f"    Line {line_index}: {line.plain_text!r} "
                    f"font {', '.join(names)} (run cutter {cutter_text}, "
                    f"line cutter {line.cutter_diameter:.3f}in)"
                )
            if entry[4]:
                for fix in rendered.descender_adjustments_by_line.get(line_index, ()):
                    if fix.reciprocal:
                        lines.append(
                            f"    Line {line_index}: {line.plain_text!r} glyph {fix.char!r} "
                            f"shares relief, top-down scale {fix.scale_y:.3f} "
                            f"(gap {fix.gap_before:.3f}in -> {fix.gap_after:.3f}in)"
                        )
                        continue
                    lines.append(
                        f"    Line {line_index}: {line.plain_text!r} glyph {fix.char!r} "
                        f"descender v-scale {fix.scale_y:.3f}, raised {fix.raise_amount:.3f}in "
                        f"(gap {fix.gap_before:.3f}in -> {fix.gap_after:.3f}in)"
                    )
            # In compact mode, print spacing deviations; in full mode, they're shown in the spacing section
            if not full and effective is not None:
                lines.append(
                    f"    Line {line_index}: {line.plain_text!r} "
                    f"spacing below {effective:.3f}in (requested {requested:.3f}in)"
                )
        elif full:
            # Show compression info in full mode if present
            scale = rendered.compression_by_line.get(line_index)
            if scale is not None and scale < 1.0:
                lines.append(
                    f"    Line {line_index}: {line.plain_text!r} "
                    f"scale {scale:.3f} ({(1.0 - scale) * 100.0:.1f}% compressed)"
                )
    return lines


def format_layout_report(
    resolved_labels: Sequence[ResolvedLabel],
    rendered_by_id: Mapping[str, RenderedLabel],
    *,
    full: bool = False,
) -> list[str]:
    """Format the LAYOUT REPORT lines for a job.

    Labels are reported in ``resolved_labels`` order (content order); ids
    missing from ``rendered_by_id`` are skipped. Each label prints its
    compressed lines and, per gap, the effective spacing below the line
    (with the requested value in parentheses whenever the render-time
    margin clamp changed it).

    Args:
        resolved_labels: The job's resolved labels, in content order. Only
            used for the iteration order and label ids; the report reads
            text and requested spacing from each rendered label's
            ``source_label`` (the adjusted clone).
        rendered_by_id: The render cache (``PerCutterExport.rendered_labels``)
            keyed by label id.
        full: When True, print every line's spacing gap and a summary line
            for untouched labels. When False (compact, the CLI default),
            print only labels with findings.

    Returns:
        The report lines (without a banner); empty when there is nothing
        to report in compact mode, or when no rendered labels are available.
    """
    lines: list[str] = []
    for label in resolved_labels:
        rendered = rendered_by_id.get(label.id)
        if rendered is None:
            continue
        lines.extend(_label_lines(rendered, full=full))
    if lines and not has_layout_findings(resolved_labels, rendered_by_id):
        lines.append("")
        lines.append(_NO_FINDINGS_FOOTER)
    return lines


def has_layout_findings(
    resolved_labels: Sequence[ResolvedLabel],
    rendered_by_id: Mapping[str, RenderedLabel],
) -> bool:
    """Return whether any label was compressed or had spacing adjusted.

    Args:
        resolved_labels: The job's resolved labels, in content order.
        rendered_by_id: The render cache keyed by label id.

    Returns:
        ``True`` when at least one label carries a compressed line or a
        spacing gap that deviates from the requested spacing.
    """
    return any(
        _label_findings(rendered)
        for label in resolved_labels
        if (rendered := rendered_by_id.get(label.id)) is not None
    )
