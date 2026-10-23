"""Unit tests for the structured GENERATION REPORT.

``plt_optimizer.generate.generation_report`` is the machine-readable
surface the ``generate`` CLI writes into ``<out>/generation_report/``. The
tests build ``ResolvedLabel`` / ``RenderedLabel`` fixtures directly (no
rendering) and pin:

- the JSON document shape (``job`` / ``summary`` / ``timings`` / ``outputs``
  / ``labels`` / ``warnings`` / ``errors``),
- the typed per-line findings (one entry per applied effect),
- the log capture (WARNING/ERROR records, with the stage timing lines
  diverted into their own list),
- the human-readable summary and the on-disk artifacts.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from plt_optimizer.generate.generation_report import (
    REPORT_JSON_NAME,
    REPORT_SCHEMA_VERSION,
    REPORT_SUMMARY_NAME,
    ReportCapture,
    build_report,
    format_summary,
    report_dir_for,
    utc_now_iso,
    write_report,
)
from plt_optimizer.generate.label_renderer import DescenderFix, RenderedLabel
from plt_optimizer.generate.layout_report import LineFinding, collect_line_findings
from plt_optimizer.generate.resolution import ResolvedLabel, ResolvedTextLine
from plt_optimizer.utils.stage_timing import StageTimings


def _line(text: str, line_spacing: float = 0.0) -> ResolvedTextLine:
    """Build a minimal resolved text line with the given requested spacing."""
    return ResolvedTextLine(
        text=text,
        nominal_text_height=0.3,
        toolpath_text_height=0.27,
        cutter_diameter=0.03,
        character_spacing=0.0,
        line_spacing=line_spacing,
        max_h_compress=0.0,
        text_h_alignment="center",
    )


def _label(
    label_id: str,
    lines: list[ResolvedTextLine],
    *,
    compression: dict[int, float] | None = None,
    spacing: dict[int, float] | None = None,
    downsizes: dict[int, tuple[float, float]] | None = None,
    descenders: dict[int, tuple[DescenderFix, ...]] | None = None,
    holes: list | None = None,
    hole_adjust: bool = False,
) -> tuple[ResolvedLabel, RenderedLabel]:
    """Build a (resolved, rendered) pair carrying the given render-time maps."""
    resolved = ResolvedLabel(
        id=label_id,
        count=1,
        width=4.0,
        height=2.0,
        margin=0.1,
        h_margin=0.1,
        v_margin=0.1,
        holes=list(holes or []),
        content=lines,
        cutter_downsize_by_line=dict(downsizes or {}),
        hole_adjust_for_cutter=hole_adjust,
    )
    rendered = RenderedLabel(
        source_label=resolved,
        plt_content="IN;PA;SP;",
        x_min=0.0,
        y_min=0.0,
        x_max=4.0,
        y_max=2.0,
        width=4.0,
        height=2.0,
        compression_by_line=dict(compression or {}),
        line_spacing_by_line=dict(spacing or {}),
        descender_adjustments_by_line=dict(descenders or {}),
    )
    return resolved, rendered


def _fix(char: str = "p") -> DescenderFix:
    """Build a descending-character relief record."""
    return DescenderFix(
        line_index=0,
        glyph_index=2,
        char=char,
        scale_y=0.6,
        raise_amount=0.071,
        gap_before=-0.077,
        gap_after=0.045,
    )


def _record(level: int, message: str, name: str = "plt_optimizer.generate.test") -> logging.LogRecord:
    """Build a log record for the capture tests."""
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


class TestReportCapture:
    """The capture keeps WARNING/ERROR records and diverts stage timings."""

    def test_captures_warning_and_error_records(self) -> None:
        """WARNING and ERROR records are kept; INFO records are dropped."""
        capture = ReportCapture()
        logger = logging.getLogger("plt_optimizer.generate.test_capture")
        logger.setLevel(logging.INFO)

        capture.records = []
        logger.addHandler(capture)
        try:
            logger.warning("a warning")
            logger.error("an error")
            logger.info("an info")
        finally:
            logger.removeHandler(capture)

        assert capture.messages() == ["a warning", "an error"]
        assert len(capture.warnings()) == 1
        assert len(capture.errors()) == 1

    def test_stage_timings_are_diverted(self) -> None:
        """A stage timing line lands in stage_records, not in records."""
        capture = ReportCapture()
        capture.emit(_record(logging.INFO, "stage parse: 0.123s"))

        assert capture.records == []
        assert len(capture.stage_records) == 1

    def test_close_detaches_the_handler(self) -> None:
        """close() removes the handler from the logger it was attached to."""
        capture = ReportCapture()
        logger = logging.getLogger("plt_optimizer.generate.test_close")
        logger.addHandler(capture)
        assert capture in logger.handlers

        capture.close()

        assert capture not in logger.handlers
        # Closing twice must not raise.
        capture.close()


class TestBuildReportShape:
    """The JSON document has the documented top-level shape."""

    def test_top_level_sections(self, tmp_path: Path) -> None:
        """The report carries exactly the documented sections."""
        resolved, rendered = _label("l1", [_line("A")])
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            resolved_labels=[resolved],
            rendered_by_id={"l1": rendered},
        )

        assert list(report) == [
            "schema_version",
            "job",
            "summary",
            "timings",
            "outputs",
            "labels",
            "warnings",
            "errors",
        ]
        assert report["schema_version"] == REPORT_SCHEMA_VERSION
        assert report["job"]["status"] == "success"
        assert report["job"]["error"] is None

    def test_report_dir_is_a_sibling_of_plt_and_pdf(self, tmp_path: Path) -> None:
        """The report directory sits next to the plt/ and pdf/ directories."""
        report_dir = report_dir_for(tmp_path)

        assert report_dir == tmp_path / "generation_report"
        assert report_dir.parent == tmp_path

    def test_failure_report_carries_the_error(self, tmp_path: Path) -> None:
        """A failed job records the error type, message and stage."""
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            status="failed",
            error={"type": "LabelRenderError", "message": "boom", "stage": "export"},
        )

        assert report["job"]["status"] == "failed"
        assert report["job"]["error"] == {
            "type": "LabelRenderError",
            "message": "boom",
            "stage": "export",
        }

    def test_summary_counts(self, tmp_path: Path) -> None:
        """The summary counts the labels, lines and applied effects."""
        resolved, rendered = _label(
            "l1",
            [_line("WIDE"), _line("B")],
            compression={0: 0.87},
            downsizes={0: (0.06, 0.045)},
        )
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            resolved_labels=[resolved],
            rendered_by_id={"l1": rendered},
        )

        summary = report["summary"]
        assert summary["labels"] == 1
        assert summary["lines"] == 2
        assert summary["labels_with_findings"] == 1
        assert summary["compressed_lines"] == 1
        assert summary["downsized_cutters"] == 1
        assert summary["descender_reliefs"] == 0
        assert summary["warnings"] == 0
        assert summary["errors"] == 0

    def test_timings_are_summed_per_stage(self, tmp_path: Path) -> None:
        """A stage emitted twice is summed in the timings map."""
        stage_records = [
            _record(logging.INFO, "stage export: 1.500s"),
            _record(logging.INFO, "stage export: 2.000s"),
        ]
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            stage_records=stage_records,
        )

        assert report["timings"] == {"export": 3.5}

    def test_warnings_and_errors_are_attributed(self, tmp_path: Path) -> None:
        """A record's label id and line index are parsed best-effort."""
        records = [
            _record(logging.WARNING, "Label l1: text line 2 compressed to fit margin"),
            _record(logging.ERROR, "Something else went wrong"),
        ]
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            records=records,
        )

        assert len(report["warnings"]) == 1
        assert report["warnings"][0]["label_id"] == "l1"
        assert report["warnings"][0]["line_index"] == 2
        assert len(report["errors"]) == 1
        assert report["errors"][0]["label_id"] is None

    def test_explicit_collector_wins_over_the_log_records(
        self, tmp_path: Path
    ) -> None:
        """The explicit collector is preferred over the log-derived timings.

        The stage timing *records* are INFO level, so they never exist
        when the pipeline runs at WARNING. The collector receives the
        measurements directly, so the report keeps them either way.
        """
        timings = StageTimings()
        timings.record("export", 4.5)
        timings.record("export", 0.5)
        stage_records = [_record(logging.INFO, "stage parse: 0.100s")]

        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            stage_records=stage_records,
            timings=timings,
        )

        assert report["timings"] == {"export": 5.0}

    def test_log_records_are_the_fallback(self, tmp_path: Path) -> None:
        """Without a collector the timings come from the log records."""
        stage_records = [
            _record(logging.INFO, "stage export: 1.500s"),
            _record(logging.INFO, "stage export: 2.000s"),
        ]

        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            stage_records=stage_records,
        )

        assert report["timings"] == {"export": 3.5}


class TestLineFindings:
    """Each applied effect becomes its own typed JSON finding."""

    def test_compression_finding(self, tmp_path: Path) -> None:
        """A compressed line records its scale and the percentage."""
        resolved, rendered = _label("l1", [_line("WIDE")], compression={0: 0.87})
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            resolved_labels=[resolved],
            rendered_by_id={"l1": rendered},
        )

        findings = report["labels"][0]["lines"][0]["findings"]
        assert findings == [
            {"type": "compression", "scale": 0.87, "percent_compressed": 13.0}
        ]

    def test_spacing_finding(self, tmp_path: Path) -> None:
        """A clamped gap records the requested and effective values."""
        resolved, rendered = _label("l1", [_line("A", 0.5), _line("B")], spacing={0: 0.2})
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            resolved_labels=[resolved],
            rendered_by_id={"l1": rendered},
        )

        findings = report["labels"][0]["lines"][0]["findings"]
        assert findings == [
            {"type": "spacing", "requested": 0.5, "effective": 0.2}
        ]

    def test_cutter_downsize_finding(self, tmp_path: Path) -> None:
        """A downsized cutter records the original and the final diameter."""
        resolved, rendered = _label("l1", [_line("A")], downsizes={0: (0.06, 0.045)})
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            resolved_labels=[resolved],
            rendered_by_id={"l1": rendered},
        )

        findings = report["labels"][0]["lines"][0]["findings"]
        assert findings == [
            {"type": "cutter_downsize", "original": 0.06, "final": 0.045}
        ]

    def test_descender_relief_findings(self, tmp_path: Path) -> None:
        """Each relieved glyph becomes its own finding entry."""
        resolved, rendered = _label("l1", [_line("pg")], descenders={0: (_fix("g"),)})
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            resolved_labels=[resolved],
            rendered_by_id={"l1": rendered},
        )

        findings = report["labels"][0]["lines"][0]["findings"]
        assert len(findings) == 1
        assert findings[0]["type"] == "descender_relief"
        assert findings[0]["char"] == "g"
        assert findings[0]["scale_y"] == 0.6
        assert findings[0]["reciprocal"] is False

    def test_untouched_line_has_no_findings(self, tmp_path: Path) -> None:
        """A line with no applied effect carries an empty finding list."""
        resolved, rendered = _label("l1", [_line("A")])
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            resolved_labels=[resolved],
            rendered_by_id={"l1": rendered},
        )

        assert report["labels"][0]["lines"][0]["findings"] == []

    def test_line_records_its_typography(self, tmp_path: Path) -> None:
        """Each line carries its own resolved typography."""
        resolved, rendered = _label("l1", [_line("A")])
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            resolved_labels=[resolved],
            rendered_by_id={"l1": rendered},
        )

        line = report["labels"][0]["lines"][0]
        assert line["index"] == 0
        assert line["plain_text"] == "A"
        assert line["cutter_diameter"] == 0.03
        assert line["nominal_text_height"] == 0.3
        assert line["text_h_alignment"] == "center"

    def test_floats_are_rounded(self, tmp_path: Path) -> None:
        """Every float in the report is rounded to the fixed precision."""
        resolved, rendered = _label("l1", [_line("A")], compression={0: 1.0 / 3.0})
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            resolved_labels=[resolved],
            rendered_by_id={"l1": rendered},
        )

        scale = report["labels"][0]["lines"][0]["findings"][0]["scale"]
        assert scale == round(1.0 / 3.0, 6)


class TestLineFindingDataclass:
    """The structured finding API the report is built on."""

    def test_has_effect_is_false_for_a_bare_finding(self) -> None:
        """A finding with no applied effect reports no effect."""
        assert not LineFinding(line_index=0, text="A").has_effect

    def test_has_effect_is_true_for_each_field(self) -> None:
        """Every optional field marks the finding as carrying an effect."""
        assert LineFinding(line_index=0, text="A", compression=0.9).has_effect
        assert LineFinding(line_index=0, text="A", requested_spacing=0.1).has_effect
        assert LineFinding(line_index=0, text="A", effective_spacing=0.1).has_effect
        assert LineFinding(line_index=0, text="A", downsize=(0.06, 0.045)).has_effect
        assert LineFinding(line_index=0, text="A", relief=(_fix(),)).has_effect
        assert LineFinding(line_index=0, text="A", bold=(0.03, 0.06)).has_effect
        assert LineFinding(line_index=0, text="A", markup_scale=(1.0, 2.0)).has_effect
        assert LineFinding(line_index=0, text="A", sup_sub=(0.0, 0.5)).has_effect
        assert LineFinding(line_index=0, text="A", font_runs=(("Arial",), (0.03,))).has_effect
        assert (
            LineFinding(line_index=0, text="A", cutter_runs=((0.03,), (0.03,))).has_effect
        )
        assert (
            LineFinding(line_index=0, text="A", original_cutter=(3, (0.03,))).has_effect
        )

    def test_collect_line_findings_matches_the_report(self) -> None:
        """The structured findings are the same ones the text report prints."""
        resolved, rendered = _label("l1", [_line("WIDE")], compression={0: 0.87})

        findings = collect_line_findings(rendered)

        assert len(findings) == 1
        assert findings[0].line_index == 0
        assert findings[0].text == "WIDE"
        assert findings[0].compression == 0.87


class TestFormatSummary:
    """The human-readable summary."""

    def test_summary_contains_the_headline_data(self, tmp_path: Path) -> None:
        """The summary names the job, its status and its counts."""
        resolved, rendered = _label("l1", [_line("WIDE")], compression={0: 0.87})
        report = build_report(
            job_name="Coolsys Walmart",
            job_id="Coolsys_Walmart",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            resolved_labels=[resolved],
            rendered_by_id={"l1": rendered},
            plt_files=[tmp_path / "plt" / "60_txt_job.plt"],
        )

        text = format_summary(report)

        assert "Generation report: Coolsys Walmart" in text
        assert "status:    success" in text
        assert "labels=1" in text
        assert "compressed lines=1" in text
        assert "60_txt_job.plt" in text

    def test_summary_names_the_failure(self, tmp_path: Path) -> None:
        """A failed job's summary states the error and its stage."""
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            status="failed",
            error={"type": "LabelRenderError", "message": "boom", "stage": "export"},
        )

        text = format_summary(report)

        assert "status:    failed" in text
        assert "LabelRenderError: boom" in text
        assert "stage: export" in text

    def test_summary_caps_the_findings_list(self, tmp_path: Path) -> None:
        """A long findings list is truncated with a pointer to the JSON."""
        lines = [_line(f"L{i}") for i in range(60)]
        resolved, rendered = _label("l1", lines, compression=dict.fromkeys(range(60), 0.9))
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            resolved_labels=[resolved],
            rendered_by_id={"l1": rendered},
        )

        text = format_summary(report)

        assert "and 10 more" in text
        assert REPORT_JSON_NAME in text


class TestWriteReport:
    """The on-disk artifacts."""

    def test_writes_both_artifacts(self, tmp_path: Path) -> None:
        """Both the JSON document and the text summary are written."""
        report_dir = report_dir_for(tmp_path)
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir,
        )

        json_path, summary_path = write_report(report_dir, report)

        assert json_path == report_dir / REPORT_JSON_NAME
        assert summary_path == report_dir / REPORT_SUMMARY_NAME
        assert json_path.is_file()
        assert summary_path.is_file()

    def test_json_round_trips(self, tmp_path: Path) -> None:
        """The written JSON parses back to the same document."""
        report_dir = report_dir_for(tmp_path)
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir,
        )

        json_path, _ = write_report(report_dir, report)

        assert json.loads(json_path.read_text(encoding="utf-8")) == report

    def test_creates_the_directory_on_demand(self, tmp_path: Path) -> None:
        """The report directory is created when it does not exist."""
        report_dir = tmp_path / "nested" / "generation_report"
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir,
        )

        write_report(report_dir, report)

        assert report_dir.is_dir()


class TestUtcNowIso:
    """The timestamp helper."""

    def test_returns_an_iso_timestamp(self) -> None:
        """The timestamp is an ISO 8601 string."""
        stamp = utc_now_iso()

        assert isinstance(stamp, str)
        assert "T" in stamp


class TestVCenterFinding:
    """The typed ``v_center`` JSON finding entry."""

    def test_the_tag_records_a_typed_entry(self, tmp_path: Path) -> None:
        """A <vc> run emits {type, char_count, chars} in the JSON report."""
        line = _line("A-A").with_markup("A<vc>-</vc>A")
        resolved, rendered = _label("l1", [line])
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            resolved_labels=[resolved],
            rendered_by_id={"l1": rendered},
        )

        findings = report["labels"][0]["lines"][0]["findings"]
        assert findings == [{"type": "v_center", "char_count": 1, "chars": ["-"]}]

    def test_has_effect_is_true_for_the_v_center_field(self) -> None:
        """The v_center field marks the finding as carrying an effect."""
        assert LineFinding(line_index=0, text="A", v_center=(2, ("-", ":"))).has_effect

    def test_the_summary_names_the_centring(self, tmp_path: Path) -> None:
        """summary.txt renders the v_center finding line."""
        line = _line("A-A").with_markup("A<vc>-</vc>A")
        resolved, rendered = _label("l1", [line])
        report = build_report(
            job_name="Job",
            job_id="Job",
            spec_path=tmp_path / "spec.yaml",
            output_dir=tmp_path,
            report_dir=report_dir_for(tmp_path),
            resolved_labels=[resolved],
            rendered_by_id={"l1": rendered},
        )

        summary = format_summary(report)

        assert "v-centered 1 chars ('-')" in summary
