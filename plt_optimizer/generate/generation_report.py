"""Structured generation report for the ``generate`` pipeline.

The ``generate`` CLI deliberately keeps its stdout almost empty (one summary
line) so an agent driving the pipeline is not flooded with progress chatter.
Everything the pipeline learned about the job -- which lines were
compressed, which cutters were downsized, which lines triggered warnings,
why the job failed -- is written here instead, into a
``generation_report/`` directory next to the generated ``plt/`` and ``pdf/``
directories.

Two artifacts are produced:

- ``report.json`` -- the machine-readable surface. One nested document:
  ``job`` (metadata, status, error, timings), ``summary`` (counts),
  ``outputs`` (written files and plate placements), ``labels[]`` ->
  ``lines[]`` -> ``findings[]`` (the per-line render-time effects) and
  ``warnings[]`` / ``errors[]`` (the captured log records). An agent can
  query it directly with ``jq`` or any JSON reader.
- ``summary.txt`` -- the human-readable surface. A short prose digest of the
  same data: the job header, the headline counts, the findings (capped) and
  the warnings/errors.

The per-line findings come from
:func:`plt_optimizer.generate.layout_report.collect_line_findings`, so the
JSON and the text report describe the same effects and can never diverge.

The module is deliberately import-light (stdlib only at runtime; the
pipeline types are ``TYPE_CHECKING``-only imports), so it is safe on the
CLI's lazy-import path and unit-testable without matplotlib.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Set, Tuple

from plt_optimizer.generate.layout_report import LineFinding, collect_line_findings
from plt_optimizer.utils.stage_timing import STAGE_LINE_PATTERN, StageTimings

if TYPE_CHECKING:
    from plt_optimizer.generate.label_renderer import RenderedLabel
    from plt_optimizer.generate.packing.layout import PackedPlate
    from plt_optimizer.generate.resolution import ResolvedLabel

#: Version of the ``report.json`` document shape. Bump when a consumer
#: contract changes; consumers should check this before relying on a field.
REPORT_SCHEMA_VERSION: int = 1

#: Name of the report directory created next to the ``plt/`` and ``pdf/``
#: output directories.
REPORT_DIR_NAME: str = "generation_report"

#: File name of the machine-readable report.
REPORT_JSON_NAME: str = "report.json"

#: File name of the human-readable report.
REPORT_SUMMARY_NAME: str = "summary.txt"

#: Maximum number of findings listed in ``summary.txt`` before the list is
#: truncated with a pointer to ``report.json``.
SUMMARY_FINDING_LIMIT: int = 50

#: Maximum number of warnings/errors listed in ``summary.txt``.
SUMMARY_MESSAGE_LIMIT: int = 50

#: Number of decimals every float in the JSON report is rounded to. Keeps
#: two identical runs byte-comparable apart from the timestamp fields.
FLOAT_DECIMALS: int = 6

#: Pattern matching the "Label <id>:" prefix the pipeline's log messages
#: use, so a captured record can be attributed to a label.
_LABEL_MESSAGE_RE = re.compile(r"^Label (?P<label_id>[^:]+):")

#: Pattern matching the "text line N" mention in a log message, so a
#: captured record can be attributed to a line.
_LINE_MESSAGE_RE = re.compile(r"text line(?: index)? (?P<line_index>\d+)")


def _round(value: Optional[float]) -> Optional[float]:
    """Round a float for the JSON report.

    Args:
        value: The value to round, or ``None``.

    Returns:
        The value rounded to :data:`FLOAT_DECIMALS`, or ``None``.
    """
    if value is None:
        return None
    return round(float(value), FLOAT_DECIMALS)


def _round_all(values: Optional[Sequence[float]]) -> Optional[List[float]]:
    """Round a sequence of floats for the JSON report.

    Args:
        values: The values to round, or ``None``.

    Returns:
        A list of rounded values, or ``None``.
    """
    if values is None:
        return None
    return [round(float(value), FLOAT_DECIMALS) for value in values]


class ReportCapture(logging.Handler):
    """A logging handler that accumulates the pipeline's WARNING/ERROR records.

    The handler is attached to the ``plt_optimizer`` logger, so every child
    logger in the tree (``plt_optimizer.generate.*`` and friends) propagates
    its records into it. Only records at WARNING and above are kept: those
    are the ones the operator (or the calling agent) must see, and they are
    exactly what the CLI's console handler prints to stderr.

    The capture is the mechanism that lets the report carry the *why* of a
    failed or degraded job without the operator having to read the log file.

    This is a plain class (not a dataclass) on purpose: a dataclass with
    ``eq=True`` is unhashable, and :class:`logging.Handler` registers
    itself in a :class:`weakref.WeakSet` whose hash delegates to the
    referent -- an unhashable handler cannot be constructed.

    The stage timing lines (``stage <name>: <s>s``) are captured too, so
    the report can carry the pipeline's per-stage wall clock. They are
    matched by the :data:`STAGE_LINE_PATTERN` contract and are NOT counted
    as warnings.

    Attributes:
        records: The captured WARNING/ERROR records, in emission order.
        stage_records: The captured stage timing records, in emission order.
    """

    def __init__(self, level: int = logging.INFO) -> None:
        """Initialize the capture.

        The handler's level is INFO so the stage timing lines reach it, but
        only the WARNING/ERROR records are *kept* (the stage lines are
        diverted into :attr:`stage_records`).

        Args:
            level: The minimum level to capture (default ``INFO``).
        """
        super().__init__(level=level)
        self.records: List[logging.LogRecord] = []
        self.stage_records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Capture one log record.

        The stage timing lines are kept separately (they are INFO records,
        below the capture threshold, but the report wants the per-stage
        wall clock).

        Args:
            record: The record emitted by the logging system.
        """
        if STAGE_LINE_PATTERN.match(record.getMessage()) is not None:
            self.stage_records.append(record)
            return
        if record.levelno >= logging.WARNING:
            self.records.append(record)

    def close(self) -> None:
        """Detach the handler from its logger and release it.

        Calling this twice is safe. The handler is removed from every
        logger it was attached to, so a capture never outlives the run
        that created it (in-process reuse included).
        """
        for logger in list(logging.Logger.manager.loggerDict.values()):
            if isinstance(logger, logging.Logger) and self in logger.handlers:
                logger.removeHandler(self)
        super().close()

    def messages(self) -> List[str]:
        """Return the captured messages, in emission order.

        Returns:
            One formatted message per captured record.
        """
        return [record.getMessage() for record in self.records]

    def warnings(self) -> List[logging.LogRecord]:
        """Return the captured WARNING records.

        Returns:
            The captured records at WARNING level.
        """
        return [record for record in self.records if record.levelno == logging.WARNING]

    def errors(self) -> List[logging.LogRecord]:
        """Return the captured ERROR (and above) records.

        Returns:
            The captured records at ERROR level or higher.
        """
        return [record for record in self.records if record.levelno >= logging.ERROR]


def _attribute_record(record: logging.LogRecord) -> Dict[str, Any]:
    """Describe one captured log record as a JSON-ready mapping.

    The label id and line index are **best-effort** attributions parsed out
    of the message: the pipeline's messages conventionally start with
    ``Label <id>:`` and mention the offending line. The message itself is
    always included verbatim, so nothing is lost when the heuristic misses.

    Args:
        record: The captured log record.

    Returns:
        A JSON-ready mapping describing the record.
    """
    message = record.getMessage()
    label_match = _LABEL_MESSAGE_RE.match(message)
    line_match = _LINE_MESSAGE_RE.search(message)
    return {
        "level": record.levelname,
        "logger": record.name,
        "message": message,
        "label_id": label_match.group("label_id") if label_match else None,
        "line_index": int(line_match.group("line_index")) if line_match else None,
    }


def _collect_timings(
    records: Sequence[logging.LogRecord],
    timings: Optional[StageTimings] = None,
) -> Dict[str, float]:
    """Extract the pipeline's stage timings for the report.

    The explicit :class:`~plt_optimizer.utils.stage_timing.StageTimings`
    collector is preferred: it receives the elapsed seconds directly, so
    the numbers survive a logger running above INFO (the generate CLI
    runs at WARNING, where the INFO stage *record* is never created).
    The log records are the fallback -- the ``stage <name>: <s>s`` line
    format is the contract of
    :mod:`plt_optimizer.utils.stage_timing`, so the report reuses it
    instead of maintaining a second timing mechanism.

    Args:
        records: The captured log records.
        timings: The explicit collector, when the run provided one.

    Returns:
        A mapping of stage name to elapsed seconds. A stage emitted more
        than once (the per-plate/per-layer stages) is summed.
    """
    if timings is not None:
        return {name: round(seconds, FLOAT_DECIMALS) for name, seconds in timings.as_dict().items()}
    collected: Dict[str, float] = {}
    for record in records:
        match = STAGE_LINE_PATTERN.match(record.getMessage())
        if match is None:
            continue
        name = match.group("name")
        collected[name] = round(
            collected.get(name, 0.0) + float(match.group("seconds")), FLOAT_DECIMALS
        )
    return collected


def _finding_entries(finding: LineFinding) -> List[Dict[str, Any]]:
    """Render one line finding as a list of typed JSON entries.

    Each applied effect becomes its own entry, so a consumer can filter by
    ``type`` (e.g. "every compressed line") without knowing the layout of
    the other effect kinds.

    Args:
        finding: The finding to render.

    Returns:
        One mapping per applied effect; empty when the finding carries none.
    """
    entries: List[Dict[str, Any]] = []
    if finding.compression is not None:
        entries.append(
            {
                "type": "compression",
                "scale": _round(finding.compression),
                "percent_compressed": _round((1.0 - finding.compression) * 100.0),
            }
        )
    if finding.requested_spacing is not None and finding.effective_spacing is not None:
        entries.append(
            {
                "type": "spacing",
                "requested": _round(finding.requested_spacing),
                "effective": _round(finding.effective_spacing),
            }
        )
    if finding.downsize is not None:
        original, final = finding.downsize
        entries.append(
            {
                "type": "cutter_downsize",
                "original": _round(original),
                "final": _round(final),
            }
        )
    for fix in finding.relief:
        entries.append(
            {
                "type": "descender_relief",
                "char": fix.char,
                "scale_y": _round(fix.scale_y),
                "raise_amount": _round(fix.raise_amount),
                "gap_before": _round(fix.gap_before),
                "gap_after": _round(fix.gap_after),
                "reciprocal": fix.reciprocal,
            }
        )
    if finding.bold is not None:
        plain, bold = finding.bold
        entries.append(
            {
                "type": "bold_cutter",
                "plain": _round(plain),
                "bold": _round(bold),
            }
        )
    if finding.markup_scale is not None:
        low, high = finding.markup_scale
        entries.append(
            {
                "type": "scale_markup",
                "min": _round(low),
                "max": _round(high),
            }
        )
    if finding.sup_sub is not None:
        low, high = finding.sup_sub
        entries.append(
            {
                "type": "sup_sub_shift",
                "min": _round(low),
                "max": _round(high),
            }
        )
    if finding.font_runs is not None:
        names, cutters = finding.font_runs
        entries.append(
            {
                "type": "font_run",
                "fonts": list(names),
                "cutters": _round_all(cutters),
            }
        )
    if finding.cutter_runs is not None:
        requested, engraved = finding.cutter_runs
        entries.append(
            {
                "type": "cutter_markup",
                "requested": _round_all(requested),
                "engraved": _round_all(engraved),
            }
        )
    if finding.original_cutter is not None:
        count, cutters = finding.original_cutter
        entries.append(
            {
                "type": "original_cutter",
                "char_count": count,
                "cutters": _round_all(cutters),
            }
        )
    if finding.v_center is not None:
        count, chars = finding.v_center
        entries.append(
            {
                "type": "v_center",
                "char_count": count,
                "chars": list(chars),
            }
        )
    if finding.text_color_runs is not None:
        count, colors = finding.text_color_runs
        entries.append(
            {
                "type": "text_color_markup",
                "char_count": count,
                "colors": list(colors),
            }
        )
    if finding.list_marker is not None:
        marker_text, indent_left = finding.list_marker
        entries.append(
            {
                "type": "list_marker",
                "marker_text": marker_text,
                "indent_left": _round(indent_left),
            }
        )
    return entries


def _label_entries(
    resolved: ResolvedLabel,
    rendered: Optional[RenderedLabel],
) -> Dict[str, Any]:
    """Build the JSON entry for one label.

    Args:
        resolved: The resolved label.
        rendered: The rendered label (the render cache entry), or ``None``
            when the label was never rendered (a job that failed before
            the layout stage).

    Returns:
        A JSON-ready mapping describing the label and its lines.
    """
    source = rendered.source_label if rendered is not None else resolved
    findings_by_line: Dict[int, LineFinding] = {}
    if rendered is not None:
        findings_by_line = {
            finding.line_index: finding for finding in collect_line_findings(rendered)
        }

    lines: List[Dict[str, Any]] = []
    for index, line in enumerate(source.content):
        finding = findings_by_line.get(index)
        effective_spacing = (
            rendered.line_spacing_by_line.get(index) if rendered is not None else None
        )
        compression = rendered.compression_by_line.get(index) if rendered is not None else None
        lines.append(
            {
                "index": index,
                "text": line.text,
                "plain_text": line.plain_text,
                "marker_text": line.marker_text,
                "indent_left": _round(line.indent_left),
                "align_group_id": line.align_group_id,
                "nominal_text_height": _round(line.nominal_text_height),
                "toolpath_text_height": _round(line.toolpath_text_height),
                "cutter_diameter": _round(line.cutter_diameter),
                # The kerf buffer the line's cascade resolved, and the width
                # its tool therefore engraves (the value every clearance and
                # the PDF proof stroke uses).
                "cutter_buffer": _round(line.cutter_buffer),
                "effective_cutter_diameter": _round(line.effective_cutter_diameter),
                "font": line.font,
                "text_color": line.text_color,
                "text_h_alignment": line.text_h_alignment,
                "requested_line_spacing": _round(line.line_spacing),
                "effective_line_spacing": _round(effective_spacing),
                "horizontal_scale": _round(compression),
                "findings": _finding_entries(finding) if finding is not None else [],
            }
        )

    holes: List[Dict[str, Any]] = []
    for hole in source.holes:
        engraved = 2.0 * _engraved_hole_radius(
            hole.diameter,
            source.effective_hole_cutter_diameter,
            source.hole_adjust_for_cutter,
        )
        holes.append(
            {
                "location": hole.location,
                "specified_diameter": _round(hole.diameter),
                "engraved_diameter": _round(engraved),
            }
        )

    label_findings: List[Dict[str, Any]] = []
    if source.hole_adjust_for_cutter and source.holes:
        for hole_entry in holes:
            label_findings.append(
                {
                    "type": "hole_adjust",
                    "specified": hole_entry["specified_diameter"],
                    "engraved": hole_entry["engraved_diameter"],
                }
            )

    return {
        "id": source.id,
        "count": source.count,
        "width": _round(source.width),
        "height": _round(source.height),
        "h_margin": _round(source.h_margin),
        "v_margin": _round(source.v_margin),
        "hole_margin": _round(source.hole_margin),
        # The physical boundary/hole tool, its kerf buffer, and the width it
        # engraves (the value the hole compensation and collision floors use).
        "hole_cutter_diameter": _round(source.hole_cutter_diameter),
        "hole_cutter_buffer": _round(source.hole_cutter_buffer),
        "effective_hole_cutter_diameter": _round(source.effective_hole_cutter_diameter),
        "material": source.material,
        "plate_id": source.plate_id,
        "has_collisions": bool(rendered.has_collisions) if rendered is not None else None,
        "collision_detected": (bool(rendered.collision_detected) if rendered is not None else None),
        "holes": holes,
        "findings": label_findings,
        "lines": lines,
    }


def _engraved_hole_radius(diameter: float, cutter: float, adjust: bool) -> float:
    """Compute the emitted drill-hole radius for the report.

    Args:
        diameter: The specified hole diameter in inches.
        cutter: The boundary/hole cutter diameter in inches.
        adjust: Whether cutter compensation is enabled.

    Returns:
        The emitted circle's radius in inches.
    """
    from plt_optimizer.generate.resolution import hole_engraved_radius

    return hole_engraved_radius(diameter, cutter, adjust)


def _plate_entries(packed_plates: Sequence[PackedPlate]) -> List[Dict[str, Any]]:
    """Build the JSON entries for the packed plates.

    Args:
        packed_plates: The packed plates from the export.

    Returns:
        One mapping per plate, in packing order.
    """
    plates: List[Dict[str, Any]] = []
    for number, plate in enumerate(packed_plates, start=1):
        plates.append(
            {
                "plate_number": number,
                "plate_id": plate.plate_id,
                "material": plate.material,
                "width": _round(plate.width),
                "height": _round(plate.height),
                "left_clearance": _round(plate.left_clearance),
                "top_clearance": _round(plate.top_clearance),
                "labels": [
                    {
                        "label_id": packed.label_id,
                        "x": _round(packed.x),
                        "y": _round(packed.y),
                        "width": _round(packed.width),
                        "height": _round(packed.height),
                        "rotated": packed.rotated,
                    }
                    for packed in plate.labels
                ],
            }
        )
    return plates


def build_report(
    *,
    job_name: str,
    job_id: str,
    spec_path: Path,
    output_dir: Path,
    report_dir: Path,
    status: str = "success",
    error: Optional[Dict[str, Any]] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    tools: Optional[Dict[str, Any]] = None,
    options: Optional[Dict[str, Any]] = None,
    plate_count: int = 0,
    resolved_labels: Sequence[ResolvedLabel] = (),
    rendered_by_id: Optional[Dict[str, RenderedLabel]] = None,
    packed_plates: Sequence[PackedPlate] = (),
    plt_files: Sequence[Path] = (),
    pdf_files: Sequence[Path] = (),
    default_pdf_files: Sequence[Path] = (),
    records: Sequence[logging.LogRecord] = (),
    stage_records: Sequence[logging.LogRecord] = (),
    timings: Optional[StageTimings] = None,
) -> Dict[str, Any]:
    """Build the JSON-ready generation report.

    Args:
        job_name: The job's name (from the YAML spec).
        job_id: The sanitized job id used in the output file names.
        spec_path: The job specification path.
        output_dir: The output directory (``plt/`` and ``pdf/`` live inside).
        report_dir: The report directory (sibling of ``plt/`` and ``pdf/``).
        status: ``"success"`` or ``"failed"``.
        error: The failure detail (``type``/``message``/``stage``) when the
            job failed; ``None`` otherwise.
        started_at: The job start timestamp (ISO 8601).
        finished_at: The job end timestamp (ISO 8601).
        duration_seconds: The job's wall-clock duration.
        tools: The cutter inventory and the boundary/hole cutter size.
        options: The CLI options the run was made with.
        plate_count: The number of plates the job declared.
        resolved_labels: The resolved labels, in content order.
        rendered_by_id: The render cache keyed by label id.
        packed_plates: The packed plates (placement report).
        plt_files: The written per-cutter PLT files.
        pdf_files: The written PDF previews.
        default_pdf_files: The written color-coded default plots.
        records: The captured log records (the ``ReportCapture`` contents).
        stage_records: The captured stage timing records.
        timings: The explicit per-stage timing collector. Preferred over
            the log records, because the stage timing *records* are INFO
            level and never exist when the logger runs at WARNING.

    Returns:
        The report document, ready to be serialized as JSON.
    """
    rendered_map = rendered_by_id or {}
    labels = [
        _label_entries(resolved, rendered_map.get(resolved.id)) for resolved in resolved_labels
    ]

    total_lines = sum(len(label["lines"]) for label in labels)
    # A label "has findings" when any of its lines carries an applied
    # effect or when it carries a label-level finding (e.g. the
    # cutter-compensated drill holes).
    labels_with_findings = sum(
        1
        for label in labels
        if label["findings"] or any(line["findings"] for line in label["lines"])
    )
    compressed_lines = 0
    downsized_lines = 0
    relief_glyphs = 0
    distinct_cutters: Set[float] = set()
    for label in labels:
        for line in label["lines"]:
            for entry in line["findings"]:
                kind = entry["type"]
                if kind == "compression":
                    compressed_lines += 1
                elif kind == "cutter_downsize":
                    downsized_lines += 1
                elif kind == "descender_relief":
                    relief_glyphs += 1
            if line["cutter_diameter"] is not None:
                distinct_cutters.add(line["cutter_diameter"])

    warnings = [
        _attribute_record(record) for record in records if record.levelno == logging.WARNING
    ]
    errors = [_attribute_record(record) for record in records if record.levelno >= logging.ERROR]

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "job": {
            "job_name": job_name,
            "job_id": job_id,
            "status": status,
            "error": error,
            "spec_path": str(spec_path),
            "output_dir": str(output_dir),
            "report_dir": str(report_dir),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_seconds": _round(duration_seconds),
            "tools": tools or {},
            "options": options or {},
        },
        "summary": {
            "plates": plate_count,
            "labels": len(labels),
            "lines": total_lines,
            "labels_with_findings": labels_with_findings,
            "compressed_lines": compressed_lines,
            "downsized_cutters": downsized_lines,
            "descender_reliefs": relief_glyphs,
            "distinct_cutters": len(distinct_cutters),
            "warnings": len(warnings),
            "errors": len(errors),
        },
        "timings": _collect_timings(stage_records, timings),
        "outputs": {
            "plt_files": [str(path) for path in plt_files],
            "pdf_files": [str(path) for path in pdf_files],
            "default_pdf_files": [str(path) for path in default_pdf_files],
            "plates": _plate_entries(packed_plates),
        },
        "labels": labels,
        "warnings": warnings,
        "errors": errors,
    }


def _format_finding_line(label_id: str, line: Dict[str, Any]) -> str:
    """Render one line's findings as a single text line.

    Args:
        label_id: The owning label's id.
        line: The line's JSON entry.

    Returns:
        A one-line description of the line's findings.
    """
    parts: List[str] = []
    for entry in line["findings"]:
        kind = entry["type"]
        if kind == "compression":
            parts.append(f"compressed to {entry['scale']:.3f}")
        elif kind == "spacing":
            parts.append(
                f"spacing {entry['effective']:.3f}in (requested {entry['requested']:.3f}in)"
            )
        elif kind == "cutter_downsize":
            parts.append(f"cutter {entry['original']:.3f}in -> {entry['final']:.3f}in")
        elif kind == "descender_relief":
            parts.append(f"relief on {entry['char']!r}")
        elif kind == "bold_cutter":
            parts.append(f"bold cutter {entry['bold']:.3f}in")
        elif kind == "scale_markup":
            parts.append(f"scale markup {entry['min']:.3f}-{entry['max']:.3f}")
        elif kind == "sup_sub_shift":
            parts.append(f"sup/sub shift {entry['min']:.3f}-{entry['max']:.3f}")
        elif kind == "font_run":
            parts.append(f"font {', '.join(entry['fonts'])}")
        elif kind == "cutter_markup":
            parts.append(f"cutter markup {entry['requested']} -> {entry['engraved']}")
        elif kind == "original_cutter":
            parts.append(f"original cutter ({entry['char_count']} chars)")
        elif kind == "v_center":
            parts.append(
                f"v-centered {entry['char_count']} chars "
                f"({', '.join(repr(char) for char in entry['chars'])})"
            )
        elif kind == "text_color_markup":
            parts.append(f"color {', '.join(entry['colors'])} ({entry['char_count']} chars)")
        elif kind == "list_marker":
            parts.append(
                f"list marker {entry['marker_text']!r} (text column {entry['indent_left']:.3f}in)"
            )
    if not parts:
        return ""
    return f"  Label {label_id} line {line['index']} {line['plain_text']!r}: " + "; ".join(parts)


def format_summary(report: Dict[str, Any]) -> str:
    """Render the human-readable summary of a report.

    Args:
        report: The report document (as built by :func:`build_report`).

    Returns:
        The summary text.
    """
    job = report.get("job", {})
    summary = report.get("summary", {})
    lines: List[str] = []
    lines.append(f"Generation report: {job.get('job_name', '')}")
    lines.append(f"  status:    {job.get('status', 'unknown')}")
    if job.get("error"):
        error = job["error"]
        lines.append(
            f"  error:     {error.get('type', 'Error')}: {error.get('message', '')}"
            f" (stage: {error.get('stage', 'unknown')})"
        )
    lines.append(f"  spec:      {job.get('spec_path', '')}")
    lines.append(f"  output:    {job.get('output_dir', '')}")
    if job.get("duration_seconds") is not None:
        lines.append(f"  duration:  {job['duration_seconds']:.3f}s")
    lines.append("")
    lines.append("Summary:")
    lines.append(
        f"  plates={summary.get('plates', 0)} labels={summary.get('labels', 0)} "
        f"lines={summary.get('lines', 0)}"
    )
    lines.append(
        f"  labels with findings={summary.get('labels_with_findings', 0)} "
        f"compressed lines={summary.get('compressed_lines', 0)} "
        f"downsized cutters={summary.get('downsized_cutters', 0)}"
    )
    lines.append(
        f"  descender reliefs={summary.get('descender_reliefs', 0)} "
        f"distinct cutters={summary.get('distinct_cutters', 0)}"
    )
    lines.append(f"  warnings={summary.get('warnings', 0)} errors={summary.get('errors', 0)}")

    outputs = report.get("outputs", {})
    plt_files = outputs.get("plt_files", [])
    pdf_files = outputs.get("pdf_files", [])
    lines.append("")
    lines.append(
        f"Outputs: {len(plt_files)} PLT file(s), {len(pdf_files)} PDF preview(s), "
        f"{len(outputs.get('plates', []))} plate(s)"
    )
    for path in plt_files:
        lines.append(f"  {path}")

    timings = report.get("timings", {})
    if timings:
        lines.append("")
        lines.append("Stage timings (seconds):")
        for name in sorted(timings):
            lines.append(f"  {name}: {timings[name]:.3f}s")

    finding_lines: List[str] = []
    for label in report.get("labels", []):
        for line in label.get("lines", []):
            rendered = _format_finding_line(label.get("id", ""), line)
            if rendered:
                finding_lines.append(rendered)
    if finding_lines:
        lines.append("")
        lines.append(f"Findings ({len(finding_lines)}):")
        for rendered in finding_lines[:SUMMARY_FINDING_LIMIT]:
            lines.append(rendered)
        if len(finding_lines) > SUMMARY_FINDING_LIMIT:
            lines.append(
                f"  ... and {len(finding_lines) - SUMMARY_FINDING_LIMIT} more "
                f"(see {REPORT_JSON_NAME})"
            )

    warnings = report.get("warnings", [])
    if warnings:
        lines.append("")
        lines.append(f"Warnings ({len(warnings)}):")
        for entry in warnings[:SUMMARY_MESSAGE_LIMIT]:
            lines.append(f"  [{entry.get('level', '')}] {entry.get('message', '')}")
        if len(warnings) > SUMMARY_MESSAGE_LIMIT:
            lines.append(
                f"  ... and {len(warnings) - SUMMARY_MESSAGE_LIMIT} more (see {REPORT_JSON_NAME})"
            )

    errors = report.get("errors", [])
    if errors:
        lines.append("")
        lines.append(f"Errors ({len(errors)}):")
        for entry in errors[:SUMMARY_MESSAGE_LIMIT]:
            lines.append(f"  [{entry.get('level', '')}] {entry.get('message', '')}")
        if len(errors) > SUMMARY_MESSAGE_LIMIT:
            lines.append(
                f"  ... and {len(errors) - SUMMARY_MESSAGE_LIMIT} more (see {REPORT_JSON_NAME})"
            )

    return "\n".join(lines) + "\n"


def write_report(report_dir: Path, report: Dict[str, Any]) -> Tuple[Path, Path]:
    """Write the report artifacts into the report directory.

    The directory is created on demand. Both files are written UTF-8: the
    JSON document (indented, so a human can read it too) and the text
    summary.

    Args:
        report_dir: The report directory to write into.
        report: The report document (as built by :func:`build_report`).

    Returns:
        The ``(json_path, summary_path)`` pair that was written.
    """
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / REPORT_JSON_NAME
    summary_path = report_dir / REPORT_SUMMARY_NAME
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    summary_path.write_text(format_summary(report), encoding="utf-8")
    return json_path, summary_path


def report_dir_for(output_dir: Path) -> Path:
    """Return the report directory for an output directory.

    Args:
        output_dir: The output directory (``plt/`` and ``pdf/`` live inside).

    Returns:
        The report directory, a sibling of ``plt/`` and ``pdf/``.
    """
    return Path(output_dir) / REPORT_DIR_NAME


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Returns:
        The timestamp, e.g. ``"2026-10-21T12:34:56.789012+00:00"``.
    """
    return datetime.now().astimezone().isoformat()
