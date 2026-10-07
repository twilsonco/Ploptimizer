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
    from plt_optimizer.generate.resolution import ResolvedLabel

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


def _label_findings(
    rendered: RenderedLabel,
) -> list[tuple[int, str, float | None, float | None, float | None, tuple[float, float] | None]]:
    """Collect the per-line findings (compression / spacing / downsize) of a label.

    Args:
        rendered: The rendered label carrying the render-time maps plus the
            (possibly adjusted) source label with the requested spacing and
            any compression-driven cutter downsizes.

    Returns:
        One ``(line_index, line_text, scale, requested, effective,
        downsize)`` tuple per finding, in content order. ``scale`` is set
        (below ``1.0``) only for compressed lines; ``requested``/``effective``
        are set only for spacing gaps that deviate from the requested value;
        ``downsize`` is the ``(original, final)`` cutter pair only for lines
        whose automatic cutter was reduced.
    """
    content = rendered.source_label.content
    downsizes = rendered.source_label.cutter_downsize_by_line
    findings: list[
        tuple[int, str, float | None, float | None, float | None, tuple[float, float] | None]
    ] = []
    for line_index in sorted(
        set(rendered.compression_by_line) | set(rendered.line_spacing_by_line) | set(downsizes)
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
        if not compressed and not deviates and downsize is None:
            continue
        findings.append(
            (
                line_index,
                line.text,
                scale if compressed else None,
                float(requested) if deviates else None,
                effective if deviates else None,
                downsize,
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
    finding_by_index = {
        index: (scale, requested, effective, downsize)
        for index, _t, scale, requested, effective, downsize in findings
    }

    if not full and not findings:
        return []

    lines = [f"Label {rendered.source_label.id}:"]
    if full and not findings and not rendered.line_spacing_by_line:
        lines.append(_UNTOUCHED_LINE)
        return lines

    for line_index, line in enumerate(content):
        entry = finding_by_index.get(line_index)
        if entry is not None:
            scale, requested, effective, downsize = entry
            if scale is not None:
                lines.append(
                    f"    Line {line_index}: {line.text!r} "
                    f"scale {scale:.3f} ({(1.0 - scale) * 100.0:.1f}% compressed)"
                )
            if downsize is not None:
                original, final = downsize
                lines.append(
                    f"    Line {line_index}: {line.text!r} "
                    f"cutter {original:.3f}in -> {final:.3f}in (downsized for compression)"
                )
            if effective is not None:
                lines.append(
                    f"    Line {line_index}: {line.text!r} "
                    f"spacing below {effective:.3f}in (requested {requested:.3f}in)"
                )
        elif full:
            effective = rendered.line_spacing_by_line.get(line_index)
            if effective is not None:
                lines.append(
                    f"    Line {line_index}: {line.text!r} spacing below {effective:.3f}in"
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
