"""Plotter-compatible output filename helpers (shared by optimize and generate).

The shop's plotter controller rejects PLT files whose *name* (including the
extension) is longer than 39 characters -- the same constraint that made the
optimize/watch pipeline truncate output names. The generate pipeline emits
per-cutter PLT files that go to the same controller, so the cap is enforced
there too. This module is the single source of truth for the limit and the
truncation rule; ``plt_optimizer.core.writer.PLTWriter`` and
``plt_optimizer.generate.vectorize`` both consume it.

Truncation rule (watch parity): the stem is cut from the right so the full
name (stem + extension) is exactly ``MAX_PLOT_FILENAME_CHARS`` long. The
directory part of the path is never touched -- the limit is a controller
filename constraint, not a filesystem path constraint.

Python 3.8 / Windows 7 import-safe: stdlib only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol

__all__ = ["MAX_PLOT_FILENAME_CHARS", "ensure_plot_filename_length"]

#: Maximum length (in characters) of an output filename *including* its
#: extension. The plotter controller errors on names longer than this
#: (35 chars + ".plt" observed on the shop machine -> 39 total).
MAX_PLOT_FILENAME_CHARS: int = 39


class _Warner(Protocol):
    """Minimal logging surface needed by :func:`ensure_plot_filename_length`.

    Structurally satisfied by :class:`logging.Logger` and
    :class:`~plt_optimizer.utils.logging.TextLogger`.
    """

    def warning(self, message: str) -> None:
        """Log a WARNING level message."""
        ...  # pragma: no cover - protocol declaration


def ensure_plot_filename_length(
    file_path: Path,
    logger: Optional[_Warner] = None,
    max_chars: int = MAX_PLOT_FILENAME_CHARS,
) -> Path:
    """Ensure the filename (with extension) is <= ``max_chars`` characters.

    If the filename exceeds the limit, it will be shortened from the right
    while preserving the extension. Logs a warning if the filename is
    modified.

    Args:
        file_path: Original file path.
        logger: Optional logger receiving the shortening warning
            (``logging.Logger`` or ``TextLogger``). ``None`` shortens
            silently.
        max_chars: Length budget for ``file_path.name``; defaults to
            :data:`MAX_PLOT_FILENAME_CHARS`.

    Returns:
        Possibly modified file path with filename <= ``max_chars``
        characters.
    """
    if len(file_path.name) <= max_chars:
        return file_path
    stem = file_path.stem[: max_chars - len(file_path.suffix)]
    new_file_path = file_path.with_name(stem + file_path.suffix)
    if logger is not None:
        logger.warning(
            f"Filename '{file_path.name}' exceeds {max_chars} characters. "
            f"Shortened to '{new_file_path.name}' for compatibility."
        )
    return new_file_path
