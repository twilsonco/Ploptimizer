"""Plotter-compatible output filename helpers (shared by optimize and generate).

The shop's plotter controller rejects PLT files whose *name* (including the
extension) is longer than 39 characters -- the same constraint that made the
optimize/watch pipeline truncate output names. The generate pipeline emits
per-cutter PLT files that go to the same controller, so the cap is enforced
there too. This module is the single source of truth for the limit, the
truncation rule and the character rules; ``plt_optimizer.core.writer.PLTWriter``,
``plt_optimizer.generate.vectorize`` and the three CLI subcommands all consume it.

Truncation rule (watch parity): the stem is cut from the right so the full
name (stem + extension) is exactly ``MAX_PLOT_FILENAME_CHARS`` long. The
directory part of the path is never touched -- the limit is a controller
filename constraint, not a filesystem path constraint.

Character rule: every filename *component* the pipelines generate (job id,
material tag, color tag, input stem) passes through
:func:`sanitize_filename_component`, so output names carry only ASCII letters,
digits and underscores -- no punctuation (underscores excepted) and no
international characters. The two rules are one contract: sanitize the
components, then cap the assembled name.

Python 3.8 / Windows 7 import-safe: stdlib only.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Optional, Protocol

__all__ = [
    "MAX_PLOT_FILENAME_CHARS",
    "ensure_plot_filename_length",
    "sanitize_filename_component",
]

#: Maximum length (in characters) of an output filename *including* its
#: extension. The plotter controller errors on names longer than this
#: (35 chars + ".plt" observed on the shop machine -> 39 total).
MAX_PLOT_FILENAME_CHARS: int = 39

#: Windows device names that cannot be used as a file stem (case-insensitive,
#: with any extension). Writing ``con.plt`` succeeds on the FAT/NTFS volume and
#: then every open of it fails, so such names get a trailing underscore.
_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

#: Runs of characters that are not ASCII letters, digits or underscores.
_UNSAFE_RUN_RE = re.compile(r"[^A-Za-z0-9_]+")

#: Runs of two or more underscores (created when a punctuation run sits next
#: to a literal underscore).
_REPEATED_UNDERSCORE_RE = re.compile(r"_{2,}")


class _Warner(Protocol):
    """Minimal logging surface needed by :func:`ensure_plot_filename_length`.

    Structurally satisfied by :class:`logging.Logger` and
    :class:`~plt_optimizer.utils.logging.TextLogger`.
    """

    def warning(self, message: str) -> None:
        """Log a WARNING level message."""
        ...  # pragma: no cover - protocol declaration


def _is_reserved_name(stem: str) -> bool:
    """Return whether a file-name stem is a Windows device name.

    Args:
        stem: File-name stem (no extension).

    Returns:
        True when the stem (case-insensitive) is reserved by Windows, e.g.
        ``"CON"``, ``"NUL"``, ``"COM1"``, ``"LPT9"``.
    """
    return stem.upper() in _WINDOWS_RESERVED_NAMES


def sanitize_filename_component(text: str, fallback: str = "job") -> str:
    """Reduce arbitrary text to a single safe filename component.

    The result contains only ASCII letters, digits and underscores: no
    punctuation (underscores excepted) and no international characters.

    Steps:

    1. Unicode NFKD normalization with non-ASCII bytes dropped, so accented
       Latin letters transliterate (``"Caf\\u00e9"`` -> ``"Cafe"``,
       ``"\\u00d1o\\u00f1o"`` -> ``"Nono"``). Scripts with no ASCII form
       (CJK, emoji, Cyrillic) contribute nothing.
    2. Every run of remaining unsafe characters collapses to a single
       underscore, and adjacent underscores are merged, so separators stay
       readable and never waste characters
       (``"2026-10-06 wb-uv"`` -> ``"2026_10_06_wb_uv"``).
    3. Leading/trailing underscores are stripped (no dangling separators).
    4. An empty result falls back to ``fallback``.
    5. A Windows reserved device name gains a trailing underscore
       (``"CON"`` -> ``"CON_"``) so the file stays openable on Windows.

    Args:
        text: Raw text (job name, material name, input file stem, ...).
        fallback: Value used when nothing survives sanitization.

    Returns:
        A non-empty, filesystem- and plotter-safe component (never the
        fallback's reserved form: the guard runs on the final value).
    """
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    sanitized = _UNSAFE_RUN_RE.sub("_", ascii_text)
    # A punctuation run adjacent to a literal underscore yields two separators.
    sanitized = _REPEATED_UNDERSCORE_RE.sub("_", sanitized).strip("_")
    if not sanitized:
        sanitized = fallback
    if _is_reserved_name(sanitized):
        sanitized += "_"
    return sanitized


def ensure_plot_filename_length(
    file_path: Path,
    logger: Optional[_Warner] = None,
    max_chars: int = MAX_PLOT_FILENAME_CHARS,
) -> Path:
    """Ensure the filename (with extension) is <= ``max_chars`` characters.

    If the filename exceeds the limit, it will be shortened from the right
    while preserving the extension. Logs a warning if the filename is
    modified.

    A stem shortened onto a Windows reserved device name (``"com1_999"`` ->
    ``"com1"``) has its last character replaced with an underscore, which
    un-reserves it without spending a character of the budget. An extension
    that alone reaches ``max_chars`` cannot be capped, so the path is returned
    unchanged with a warning.

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
    stem_budget = max_chars - len(file_path.suffix)
    if stem_budget <= 0:
        if logger is not None:
            logger.warning(
                f"Filename '{file_path.name}' exceeds {max_chars} characters, "
                f"but its extension '{file_path.suffix}' alone leaves no room "
                f"to shorten it. Name left unchanged."
            )
        return file_path
    stem = file_path.stem[:stem_budget]
    if _is_reserved_name(stem):
        # Right-truncation landed on a reserved device name; the underscore
        # keeps the name within budget (all reserved names end in alnum).
        stem = stem[:-1] + "_"
    new_file_path = file_path.with_name(stem + file_path.suffix)
    if logger is not None:
        logger.warning(
            f"Filename '{file_path.name}' exceeds {max_chars} characters. "
            f"Shortened to '{new_file_path.name}' for compatibility."
        )
    return new_file_path
