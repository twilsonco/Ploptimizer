"""Tests for the generate-pipeline stage timer (plt_optimizer.generate.stage_timing).

Pins the machine-parseable timing-line contract consumed by
``scripts/profile_generate.py``: one INFO line ``stage <name>: <seconds>s``
per completed stage, parseable by :data:`STAGE_LINE_PATTERN`.
"""

from __future__ import annotations

import logging

import pytest

from plt_optimizer.utils import stage_timing
from plt_optimizer.utils.stage_timing import (
    STAGE_LINE_PATTERN,
    STAGE_LOG_FORMAT,
    StageTimings,
    stage_timer,
)

LOGGER_NAME = "test.stage_timing"


@pytest.fixture()
def logger() -> logging.Logger:
    """A bare logger; caplog captures via root propagation."""
    return logging.getLogger(LOGGER_NAME)


def _timing_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the rendered timing lines captured in the log."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER_NAME and record.getMessage().startswith("stage ")
    ]


class TestStageTimer:
    """Behavioral contract of the stage_timer context manager."""

    def test_emits_parseable_info_line(self, logger: logging.Logger, caplog) -> None:
        """A completed stage logs exactly one INFO line matching the pattern."""
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            with stage_timer("layout_render_pack", logger):
                pass

        lines = _timing_lines(caplog)
        assert len(lines) == 1
        match = STAGE_LINE_PATTERN.match(lines[0])
        assert match is not None, f"unparseable timing line: {lines[0]!r}"
        assert match.group("name") == "layout_render_pack"
        assert float(match.group("seconds")) >= 0.0

    def test_elapsed_is_measured(self, logger: logging.Logger, caplog, monkeypatch) -> None:
        """The logged seconds equal the perf_counter delta of the stage."""
        clock = iter([100.0, 103.5])
        monkeypatch.setattr(stage_timing.time, "perf_counter", lambda: next(clock))

        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            with stage_timer("timed", logger):
                pass

        match = STAGE_LINE_PATTERN.match(_timing_lines(caplog)[0])
        assert match is not None
        assert float(match.group("seconds")) == pytest.approx(3.5)

    def test_exception_propagates_after_timing(
        self, logger: logging.Logger, caplog
    ) -> None:
        """A raising stage still emits its timing line; the error propagates."""
        with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
            with pytest.raises(RuntimeError, match="boom"):
                with stage_timer("failing", logger):
                    raise RuntimeError("boom")

        lines = _timing_lines(caplog)
        assert len(lines) == 1
        assert STAGE_LINE_PATTERN.match(lines[0]) is not None

    def test_custom_level(self, logger: logging.Logger, caplog) -> None:
        """The level kwarg controls the record's level."""
        with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
            with stage_timer("quiet", logger, level=logging.DEBUG):
                pass

        records = [r for r in caplog.records if r.name == LOGGER_NAME]
        assert [r.levelno for r in records] == [logging.DEBUG]
        assert STAGE_LINE_PATTERN.match(records[0].getMessage()) is not None

    def test_body_yields_none(self, logger: logging.Logger) -> None:
        """The context manager yields None (a plain timed block, no value)."""
        with stage_timer("body", logger) as value:
            assert value is None


class TestStageTimings:
    """The level-independent collector."""

    def test_collects_the_elapsed_seconds(self, logger: logging.Logger) -> None:
        """The collector receives the stage's elapsed seconds."""
        timings = StageTimings()
        clock = iter([100.0, 103.5])
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(stage_timing.time, "perf_counter", lambda: next(clock))
        try:
            with stage_timer("timed", logger, timings=timings):
                pass
        finally:
            monkeypatch.undo()

        assert timings.as_dict() == {"timed": pytest.approx(3.5)}

    def test_survives_a_warning_level_logger(self, logger: logging.Logger) -> None:
        """The measurements survive a logger that drops the INFO record.

        This is the generate CLI's situation: the pipeline runs at WARNING,
        so the INFO stage *record* never exists -- but the collector still
        has the numbers.
        """
        timings = StageTimings()
        logger.setLevel(logging.WARNING)
        try:
            with stage_timer("timed", logger, timings=timings):
                pass
        finally:
            logger.setLevel(logging.NOTSET)

        assert "timed" in timings.as_dict()
        assert timings.as_dict()["timed"] >= 0.0

    def test_repeated_stages_are_summed(self) -> None:
        """A stage measured more than once is summed, like the log readers."""
        timings = StageTimings()
        timings.record("export", 1.0)
        timings.record("export", 2.0)
        timings.record("parse", 0.5)

        assert timings.as_dict() == {"export": 3.0, "parse": 0.5}

    def test_order_is_first_emission_order(self) -> None:
        """The stages are reported in the order they were first seen."""
        timings = StageTimings()
        timings.record("parse", 0.1)
        timings.record("export", 0.2)
        timings.record("parse", 0.3)

        assert list(timings.as_dict()) == ["parse", "export"]

    def test_empty_collector_is_an_empty_mapping(self) -> None:
        """A collector that never saw a stage is empty."""
        assert StageTimings().as_dict() == {}


class TestStageLineContract:
    """The emitted format and the parser regex stay in lockstep."""

    def test_format_round_trips_through_pattern(self) -> None:
        """A line rendered from STAGE_LOG_FORMAT parses with the pattern."""
        line = STAGE_LOG_FORMAT % ("cutter_downsize", 12.345)
        match = STAGE_LINE_PATTERN.match(line)
        assert match is not None
        assert match.group("name") == "cutter_downsize"
        assert float(match.group("seconds")) == pytest.approx(12.345)

    def test_pattern_rejects_non_timing_lines(self) -> None:
        """Operational log lines never match the timing contract."""
        for line in [
            "routing 12.000 -> 9.000",
            "stage missing-seconds",
            "stage bad: 12s",  # no decimals
            "stage bad: abc s",
            "prefix stage x: 1.000s",
        ]:
            assert STAGE_LINE_PATTERN.match(line) is None, line
