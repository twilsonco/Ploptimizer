"""Wall-clock stage timing for the generate and optimize pipelines.

Import-light (stdlib only: ``time`` + ``logging`` + ``contextlib``), so it
is safe on the Python 3.8 / Windows 7 import path and can be imported at
module scope by the CLI, the heavy generate modules, and the core
optimization pipeline alike.

Each pipeline stage is wrapped in :func:`stage_timer`, which emits one
machine-parseable INFO line on completion::

    stage <name>: <seconds>s

The fixed format is the contract consumed by ``scripts/profile_generate.py``
and ``scripts/profile_optimize.py`` (see :data:`STAGE_LINE_PATTERN`); keep
the two in sync when changing the format. Timers measure wall-clock time
(``time.perf_counter``) and never alter pipeline behavior: the stage body
runs unmodified and exceptions propagate after the timing line is logged.

**Level independence.** The timing line is a log *record*, so the logging
system's level gates it: a pipeline running at WARNING never creates the
INFO record and the measurement is lost. A consumer that needs the numbers
regardless of the log level (the generation report) passes a
:class:`StageTimings` collector to :func:`stage_timer` and reads the
measurements from it. The log record and the collector are independent
sinks; either can be used without the other.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

#: ``logging``-style format for the emitted timing line. The exact shape is
#: pinned by :data:`STAGE_LINE_PATTERN` and the profiling script's parser.
STAGE_LOG_FORMAT: str = "stage %s: %.3fs"

#: Regex matching one rendered timing line (group ``name`` = stage name,
#: group ``seconds`` = elapsed wall-clock seconds as a decimal).
STAGE_LINE_PATTERN: re.Pattern[str] = re.compile(r"^stage (?P<name>.+): (?P<seconds>\d+\.\d+)s$")


@dataclass
class StageTimings:
    """Per-stage wall-clock timings collected independently of the log level.

    The stage timer normally reports through a log record, which the
    logging system's level gates: a pipeline running at WARNING never
    creates the INFO record, so the measurement never reaches a
    log-reading consumer. This collector is the level-independent
    alternative -- the stage timer writes the elapsed time here directly,
    so a consumer gets the numbers whatever the log level is.

    A stage emitted more than once (the per-plate and per-layer stages)
    is **summed**, matching how the log-based consumers aggregate it.

    Attributes:
        seconds: The accumulated elapsed seconds per stage name.
        order: The stage names in first-emission order.
    """

    seconds: Dict[str, float] = field(default_factory=dict)
    order: List[str] = field(default_factory=list)

    def record(self, stage: str, elapsed: float) -> None:
        """Record one stage's elapsed seconds.

        Args:
            stage: The stage name.
            elapsed: The elapsed wall-clock seconds.
        """
        if stage not in self.seconds:
            self.order.append(stage)
        self.seconds[stage] = self.seconds.get(stage, 0.0) + elapsed

    def as_dict(self) -> Dict[str, float]:
        """Return the collected timings as a plain mapping.

        Returns:
            A mapping of stage name to accumulated elapsed seconds, in
            first-emission order.
        """
        return {name: self.seconds[name] for name in self.order}


@contextmanager
def stage_timer(
    stage: str,
    logger: logging.Logger,
    *,
    level: int = logging.INFO,
    timings: Optional[StageTimings] = None,
) -> Iterator[None]:
    """Time one pipeline stage and log the elapsed wall-clock seconds.

    Args:
        stage: Machine-parseable stage name (no ``": "`` or trailing
            ``"s"`` — keep it a short snake_case identifier).
        logger: Logger receiving the timing line on stage completion.
        level: Logging level for the timing line (defaults to INFO).
        timings: Optional collector that receives the elapsed seconds
            directly, independent of the logger's level. Use it when the
            measurements must survive a WARNING-level logger.

    Yields:
        None; the ``with`` body runs as the timed stage.

    The timing line is emitted in a ``finally`` block, so a raising stage
    is still timed (the exception propagates unchanged).
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        if timings is not None:
            timings.record(stage, elapsed)
        logger.log(level, STAGE_LOG_FORMAT, stage, elapsed)
