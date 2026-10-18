"""Wall-clock stage timing for the generate pipeline.

Import-light (stdlib only: ``time`` + ``logging`` + ``contextlib``), so it
is safe on the Python 3.8 / Windows 7 import path and can be imported at
module scope by both the CLI and the heavy generate modules.

Each pipeline stage is wrapped in :func:`stage_timer`, which emits one
machine-parseable INFO line on completion::

    stage <name>: <seconds>s

The fixed format is the contract consumed by
``scripts/profile_generate.py`` (see :data:`STAGE_LINE_PATTERN`); keep the
two in sync when changing the format. Timers measure wall-clock time
(``time.perf_counter``) and never alter pipeline behaviour: the stage body
runs unmodified and exceptions propagate after the timing line is logged.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from typing import Iterator

#: ``logging``-style format for the emitted timing line. The exact shape is
#: pinned by :data:`STAGE_LINE_PATTERN` and the profiling script's parser.
STAGE_LOG_FORMAT: str = "stage %s: %.3fs"

#: Regex matching one rendered timing line (group ``name`` = stage name,
#: group ``seconds`` = elapsed wall-clock seconds as a decimal).
STAGE_LINE_PATTERN: re.Pattern[str] = re.compile(r"^stage (?P<name>.+): (?P<seconds>\d+\.\d+)s$")


@contextmanager
def stage_timer(
    stage: str,
    logger: logging.Logger,
    *,
    level: int = logging.INFO,
) -> Iterator[None]:
    """Time one pipeline stage and log the elapsed wall-clock seconds.

    Args:
        stage: Machine-parseable stage name (no ``": "`` or trailing
            ``"s"`` — keep it a short snake_case identifier).
        logger: Logger receiving the timing line on stage completion.
        level: Logging level for the timing line (defaults to INFO).

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
        logger.log(level, STAGE_LOG_FORMAT, stage, elapsed)
