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
- **Cutter markup**
  (:attr:`plt_optimizer.generate.resolution.ResolvedTextLine.cutter_overrides`):
  the cutter diameters a line's ``<c:...>`` runs declare, alongside the
  diameters they actually engrave with (snapped to the shop inventory) --
  the tool override the markup caused, which the YAML spec never states.
- **Cutter-compensated drill holes**
  (:attr:`plt_optimizer.generate.resolution.ResolvedLabel.hole_adjust_for_cutter`):
  the emitted hole diameters the cutter compensation produced (specified ->
  engraved, per distinct specified diameter) -- the size change the mechanism
  caused, which the YAML spec never states.

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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Optional, Sequence

from plt_optimizer.generate.resolution import hole_engraved_radius

if TYPE_CHECKING:
    from plt_optimizer.generate.descender_adjust import DescenderFix
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


@dataclass(frozen=True)
class LineFinding:
    """One text line's applied render-time effect, as structured data.

    A finding records a change that is invisible in the YAML spec but
    changes what is engraved. Every field is optional: a field is set only
    when that kind of effect applied to the line, so a single finding can
    carry several effects at once (a compressed line with a downsized
    cutter and a descender relief, for instance).

    This dataclass is the **structured** counterpart of the report text:
    :func:`format_layout_report` renders it as human-readable lines and
    :mod:`plt_optimizer.generate.generation_report` renders it as JSON
    entries, so the two surfaces can never diverge.

    Attributes:
        line_index: Index of the text line in the label's ``content``.
        text: The line's markup-stripped text (``plain_text``).
        compression: The *effective* horizontal scale applied while
            rendering (``1.0`` = natural width). Set only when the line
            was compressed below ``1.0``.
        requested_spacing: The requested gap below the line (inches).
            Set only when the render-time clamp changed it.
        effective_spacing: The gap below the line actually used for
            stacking (inches). Set only when it deviates from
            :attr:`requested_spacing`.
        downsize: The ``(original, final)`` cutter diameters (inches) for
            a line whose automatic cutter the compression-driven reduction
            lowered. ``None`` when the line's cutter was not downsized.
        relief: The individual descending-glyph reliefs applied to the
            line (empty when the line needed none).
        bold: The ``(plain, bold)`` cutter diameters (inches) for a line
            carrying a ``<b>`` run. ``None`` when the line has no bold run.
        markup_scale: The ``(min, max)`` ``<scale:N>`` factor range for a
            line carrying a ``<scale:N>`` run. ``None`` when absent.
        sup_sub: The ``(min, max)`` baseline-shift range (cap-height
            units) for a line carrying a ``<sup>``/``<sub>`` run. ``None``
            when absent.
        font_runs: The ``(fonts, cutters)`` a line's ``<f:...>`` runs
            engrave with. ``None`` when the line has no font run.
        cutter_runs: The ``(requested, engraved)`` cutter diameters a
            line's ``<c:...>`` runs use. ``None`` when the line has no
            cutter run.
        original_cutter: The ``(char_count, cutters)`` a line's ``<oc>``
            run pins. ``None`` when the line has no ``<oc>`` run.
        v_center: The ``(char_count, chars)`` a line vertically centres --
            the ``<vc>`` spans plus the characters of the cascading
            ``v_center_chars`` set (the count and the distinct centred
            characters, ascending). ``None`` when the line centres nothing.
        text_color_runs: The ``(char_count, colors)`` a line's ``<tc:...>``
            runs engrave on -- the count of colored characters and the
            distinct canonical colors (ascending). The color split is
            invisible in the YAML for the tagged characters (it rides the
            tag), exactly like the ``<oc>`` finding's rationale. ``None``
            when the line has no ``<tc:...>`` run.
        list_marker: The ``(marker_text, indent_left)`` pair for a line that
            engraves a list marker (a bullet or a computed counter) -- the
            marker is invisible in the YAML (it rides the list block) and the
            hanging indent is a measured column, so both are reported. The
            indent is the marker column + gap in inches. ``None`` for a plain
            line.
    """

    line_index: int
    text: str
    compression: Optional[float] = None
    requested_spacing: Optional[float] = None
    effective_spacing: Optional[float] = None
    downsize: Optional[tuple[float, float]] = None
    relief: tuple[DescenderFix, ...] = ()
    bold: Optional[tuple[float, float]] = None
    markup_scale: Optional[tuple[float, float]] = None
    sup_sub: Optional[tuple[float, float]] = None
    font_runs: Optional[tuple[tuple[str, ...], tuple[float, ...]]] = None
    cutter_runs: Optional[tuple[tuple[float, ...], tuple[float, ...]]] = None
    original_cutter: Optional[tuple[int, tuple[float, ...]]] = None
    v_center: Optional[tuple[int, tuple[str, ...]]] = None
    text_color_runs: Optional[tuple[int, tuple[str, ...]]] = None
    list_marker: Optional[tuple[str, float]] = None

    @property
    def has_effect(self) -> bool:
        """Whether this finding records at least one applied effect.

        Returns:
            ``True`` when any field other than :attr:`line_index` and
            :attr:`text` is set.
        """
        if self.compression is not None or self.requested_spacing is not None:
            return True
        if self.effective_spacing is not None or self.downsize is not None:
            return True
        if self.relief:
            return True
        return any(
            value is not None
            for value in (
                self.bold,
                self.markup_scale,
                self.sup_sub,
                self.font_runs,
                self.cutter_runs,
                self.original_cutter,
                self.v_center,
                self.text_color_runs,
                self.list_marker,
            )
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


def _original_cutter_run_info(line: ResolvedTextLine) -> tuple[int, tuple[float, ...]]:
    """Return the ``(count, cutters)`` a line's ``<oc>`` runs pin.

    The number of characters wrapped in an ``<oc>...</oc>`` span and the
    distinct tools those characters engrave with (read from the derived
    per-character cutters). The pinned tool is the line's own plain cutter,
    so the finding names the tool the markup protected -- invisible in the
    YAML, exactly like the ``<c:D>`` / ``<f:...>`` findings.
    """
    flags = line.original_cutter_flags
    cutters = line.per_char_cutters
    used = {
        cutters[index] if index < len(cutters) else line.cutter_diameter
        for index, flag in enumerate(flags)
        if flag
    }
    return sum(1 for flag in flags if flag), tuple(sorted(used))


def _v_center_info(line: ResolvedTextLine) -> tuple[int, tuple[str, ...]]:
    """Return the ``(count, chars)`` a line vertically centres.

    The number of characters the ``<vc>`` spans and the cascading
    ``v_center_chars`` set select, plus the distinct centred characters
    (ascending). The centring is invisible in the YAML for the auto-centred
    set (it rides the shop default), exactly like the ``<oc>`` finding's
    rationale.
    """
    flags = line.v_center_flags
    text = line.plain_text
    chars = {text[index] for index, flag in enumerate(flags) if flag and index < len(text)}
    return sum(1 for flag in flags if flag), tuple(sorted(chars))


def _text_color_run_info(line: ResolvedTextLine) -> tuple[int, tuple[str, ...]]:
    """Return the ``(count, colors)`` a line's ``<tc:...>`` runs engrave.

    The number of characters wrapped in a ``<tc:...>`` span and the distinct
    canonical colors those characters ride (ascending). The color split is
    invisible in the YAML for the tagged characters (it rides the tag, not
    the ``text_color`` field), exactly like the ``<oc>`` finding's rationale.
    """
    colors = line.color_runs
    used = {color for color in colors if color is not None}
    return sum(1 for color in colors if color is not None), tuple(sorted(used))


def _cutter_run_info(line: ResolvedTextLine) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Return the ``(requested, engraved)`` cutters a line's ``<c:...>`` runs use.

    Both ascending and de-duplicated, read from the derived per-character
    arrays: the diameters the tag REQUESTED and the diameters those
    characters actually engrave with (snapped to the shop inventory, so a
    request the shop does not own is visible next to the tool that runs).
    """
    overrides = line.cutter_overrides
    cutters = line.per_char_cutters
    requested: set[float] = set()
    engraved: set[float] = set()
    for index, diameter in enumerate(overrides):
        if diameter is None:
            continue
        requested.add(diameter)
        engraved.add(cutters[index] if index < len(cutters) else line.cutter_diameter)
    return tuple(sorted(requested)), tuple(sorted(engraved))


def _hole_adjust_lines(label: ResolvedLabel) -> list[str]:
    """Return the cutter-compensated drill-hole finding lines for a label.

    One line per distinct specified hole diameter: the emitted circle shrinks
    by the boundary/hole cutter diameter when ``hole_adjust_for_cutter`` is
    enabled, so the engraved hole measures the specified diameter. The YAML
    states the specified diameter only -- the finding surfaces the engraved
    size. Labels with no holes, or with the mechanism off, produce no lines.

    Args:
        label: The (source) resolved label carrying holes, the cutter
            diameter and the compensation flag.

    Returns:
        Report lines (indented like the per-line findings); empty when there
        is nothing to report.
    """
    if not label.hole_adjust_for_cutter or not label.holes:
        return []
    cutter = label.hole_cutter_diameter
    lines: list[str] = []
    seen: list[float] = []
    for hole in label.holes:
        if any(math.isclose(hole.diameter, known, rel_tol=1e-9, abs_tol=1e-9) for known in seen):
            continue
        seen.append(hole.diameter)
        engraved = 2.0 * hole_engraved_radius(hole.diameter, cutter, True)
        lines.append(f"    Holes {hole.diameter:.3f}in -> {engraved:.3f}in (cutter {cutter:.3f}in)")
    return lines


def collect_line_findings(
    rendered: RenderedLabel,
) -> list[LineFinding]:
    """Collect the per-line findings (compression / spacing / downsize / relief / bold / scale / sup-sub / font / cutter / original cutter / v-center / text color).

    Args:
        rendered: The rendered label carrying the render-time maps plus the
            (possibly adjusted) source label with the requested spacing and
            any compression-driven cutter downsizes.

    Returns:
        One :class:`LineFinding` per line that carries an applied effect, in
        content order. ``compression`` is set (below ``1.0``) only for
        compressed lines; ``requested_spacing``/``effective_spacing`` are set
        only for spacing gaps that deviate; ``downsize`` is the
        ``(original, final)`` cutter pair only for lines whose automatic
        cutter was reduced; ``relief`` holds the individual descending-glyph
        reliefs (empty for none); ``bold`` is the ``(plain, bold)`` cutter
        pair only for lines carrying a ``<b>`` run; ``markup_scale`` is the
        ``(min, max)`` ``<scale:N>`` factor range only for lines carrying a
        ``<scale:N>`` run; ``sup_sub`` is the ``(min, max)`` baseline-shift
        range (cap-height units) only for lines carrying a ``<sup>``/``<sub>``
        run; ``font_runs`` is the ``(fonts, cutters)`` pair only for lines
        carrying a ``<f:...>`` run; ``cutter_runs`` is the ``(requested,
        engraved)`` cutter pairs only for lines carrying a ``<c:...>`` run;
        ``original_cutter`` is the ``(count, cutters)`` pair only for lines
        carrying an ``<oc>`` run; ``v_center`` is the ``(count, chars)`` pair
        only for lines vertically centring characters (the ``<vc>`` spans plus
        the cascading ``v_center_chars`` set); ``text_color_runs`` is the
        ``(count, colors)`` pair only for lines carrying a ``<tc:...>`` run.
    """
    content = rendered.source_label.content
    downsizes = rendered.source_label.cutter_downsize_by_line
    reliefs = rendered.descender_adjustments_by_line
    findings: list[LineFinding] = []
    for line_index in sorted(
        set(rendered.compression_by_line)
        | set(rendered.line_spacing_by_line)
        | set(downsizes)
        | set(reliefs)
        | {index for index, line in enumerate(content) if line.bold_cutter_diameter is not None}
        | {index for index, line in enumerate(content) if line.has_scale}
        | {index for index, line in enumerate(content) if line.has_sup_sub}
        | {index for index, line in enumerate(content) if line.has_font_runs}
        | {index for index, line in enumerate(content) if line.has_cutter_runs}
        | {index for index, line in enumerate(content) if line.has_original_cutter}
        | {index for index, line in enumerate(content) if line.has_v_center}
        | {index for index, line in enumerate(content) if line.has_color_runs}
        | {index for index, line in enumerate(content) if line.marker_text}
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
        relief = tuple(reliefs.get(line_index, ()))
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
        cutter_runs: tuple[tuple[float, ...], tuple[float, ...]] | None = None
        if line.has_cutter_runs:
            cutter_runs = _cutter_run_info(line)
        original_cutter: tuple[int, tuple[float, ...]] | None = None
        if line.has_original_cutter:
            original_cutter = _original_cutter_run_info(line)
        v_center: tuple[int, tuple[str, ...]] | None = None
        if line.has_v_center:
            v_center = _v_center_info(line)
        text_color_runs: tuple[int, tuple[str, ...]] | None = None
        if line.has_color_runs:
            text_color_runs = _text_color_run_info(line)
        list_marker: tuple[str, float] | None = None
        if line.marker_text:
            list_marker = (line.marker_text, line.indent_left)
        finding = LineFinding(
            line_index=line_index,
            text=line.plain_text,
            compression=scale if compressed else None,
            requested_spacing=float(requested) if deviates else None,
            effective_spacing=effective if deviates else None,
            downsize=downsize,
            relief=relief,
            bold=bold,
            markup_scale=markup_scale,
            sup_sub=sup_sub,
            font_runs=font_runs,
            cutter_runs=cutter_runs,
            original_cutter=original_cutter,
            v_center=v_center,
            text_color_runs=text_color_runs,
            list_marker=list_marker,
        )
        if not finding.has_effect:
            continue
        findings.append(finding)
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
    findings = collect_line_findings(rendered)
    finding_by_index: dict[int, LineFinding] = {finding.line_index: finding for finding in findings}

    if not full and not findings and not _hole_adjust_lines(rendered.source_label):
        return []

    lines = [f"Label {rendered.source_label.id}:"]

    # Add margin summary line
    h_margin = rendered.source_label.h_margin
    v_margin = rendered.source_label.v_margin
    lines.append(
        f"    Margins: horizontal {h_margin:.3f}in (left/right), "
        f"vertical {v_margin:.3f}in (top/bottom)"
    )

    # Cutter-compensated drill holes (specified -> engraved diameter, per
    # distinct specified diameter) -- a label-level finding.
    lines.extend(_hole_adjust_lines(rendered.source_label))

    # Add spacing summary section showing all line gaps
    if rendered.line_spacing_by_line or content:
        spacing_lines: list[str] = []
        for line_index in range(len(content) - 1):  # n-1 gaps for n lines
            effective = rendered.line_spacing_by_line.get(line_index)
            if effective is not None:
                requested_gap: float = content[line_index].line_spacing
                if _spacing_deviation(requested_gap, effective):
                    spacing_lines.append(
                        f"      Line {line_index}: {effective:.3f}in "
                        f"(requested {requested_gap:.3f}in)"
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
            scale = entry.compression
            requested: Optional[float] = entry.requested_spacing
            effective = entry.effective_spacing
            downsize = entry.downsize
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
            bold = entry.bold
            if bold is not None:
                plain_cutter, bold_cutter = bold
                lines.append(
                    f"    Line {line_index}: {line.plain_text!r} "
                    f"bold cutter {bold_cutter:.3f}in (line cutter {plain_cutter:.3f}in)"
                )
            markup_scale = entry.markup_scale
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
            sup_sub = entry.sup_sub
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
            font_runs = entry.font_runs
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
            cutter_runs = entry.cutter_runs
            if cutter_runs is not None:
                requested_cutters, engraved_cutters = cutter_runs
                # The <c:D> tool is invisible in the YAML (it rides the tag)
                # and each declared diameter is snapped to the shop
                # inventory, so name both what was asked for and the tool
                # that actually runs.
                requested_text = (
                    f"{requested_cutters[0]:.3f}in"
                    if len(requested_cutters) == 1
                    else "-".join(f"{cutter:.3f}" for cutter in requested_cutters) + "in"
                )
                engraved_text = (
                    f"{engraved_cutters[0]:.3f}in"
                    if len(engraved_cutters) == 1
                    else "-".join(f"{cutter:.3f}" for cutter in engraved_cutters) + "in"
                )
                lines.append(
                    f"    Line {line_index}: {line.plain_text!r} "
                    f"cutter {requested_text} (engraved cutter {engraved_text}, "
                    f"line cutter {line.cutter_diameter:.3f}in)"
                )
            original_cutter = entry.original_cutter
            if original_cutter is not None:
                count, pinned_cutters = original_cutter
                # The <oc> pin is invisible in the YAML (it rides the tag), so
                # name the tool the markup protected and how many characters
                # it covers.
                cutter_text = (
                    f"{pinned_cutters[0]:.3f}in"
                    if len(pinned_cutters) == 1
                    else "-".join(f"{cutter:.3f}" for cutter in pinned_cutters) + "in"
                )
                lines.append(
                    f"    Line {line_index}: {line.plain_text!r} "
                    f"original cutter {cutter_text} ({count} chars pinned)"
                )
            v_center = entry.v_center
            if v_center is not None:
                count, chars = v_center
                # The centring rides the <vc> tag and the shop-default
                # v_center_chars set -- both invisible in the YAML -- so name
                # the characters the render raised/lowered onto the cap-band
                # midline.
                char_text = ", ".join(repr(char) for char in chars)
                lines.append(
                    f"    Line {line_index}: {line.plain_text!r} "
                    f"v-centered {count} chars ({char_text})"
                )
            text_color_runs = entry.text_color_runs
            if text_color_runs is not None:
                count, colors = text_color_runs
                # The <tc:color> split is invisible in the YAML (it rides the
                # tag, not the text_color field), so name the layers the
                # markup introduced and how many characters ride each.
                color_text = ", ".join(colors)
                lines.append(
                    f"    Line {line_index}: {line.plain_text!r} color {color_text} ({count} chars)"
                )
            list_marker = entry.list_marker
            if list_marker is not None:
                marker_text, indent_left = list_marker
                # The marker rides the list block (invisible on the item) and
                # the hanging indent is a measured column, so name both.
                lines.append(
                    f"    Line {line_index}: {line.plain_text!r} "
                    f"list marker {marker_text!r} (text column {indent_left:.3f}in)"
                )
            if entry.relief:
                for fix in entry.relief:
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
        ``True`` when at least one label carries a compressed line, a spacing
        gap that deviates from the requested spacing, or cutter-compensated
        drill holes.
    """
    return any(
        collect_line_findings(rendered) or _hole_adjust_lines(rendered.source_label)
        for label in resolved_labels
        if (rendered := rendered_by_id.get(label.id)) is not None
    )
