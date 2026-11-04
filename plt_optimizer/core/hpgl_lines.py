"""Cosmetic line-breaking for HPGL/PLT output (EngraveLab reference parity).

VisionPro and EngraveLab write every HPGL command on its own line, with a
bare pen-state command (``PD;`` / ``PU;``) glued to the arc that follows it,
e.g.::

    IN;
    VS0.80;
    PA;
    PU254.000,1080.008;
    PD254.000,1080.008;
    PD;AA254.000,1016.000,90.000;
    SP;

Our emitters produce the semantically identical compact stream
(``IN;PA;PU254,1080;PD254,1080;PD;AA254,1016,90;SP;``). This module inserts
the newlines at the write boundary so the on-disk files match the reference
tool's layout, without touching the compact in-memory intermediates that the
generate pipeline's regex transforms consume.

The transform is **parse-identity guaranteed**: the core parser tokenizes on
the ``;`` delimiter (see :data:`PLTParser.COMMAND_PATTERN`) and ignores
newlines entirely, so inserting a newline after every command -- and keeping a
bare ``PD;``/``PU;`` on the same line as the arc it governs -- cannot change
the parsed document. No command is ever merged, split, reordered, or
rewritten: a batched polyline (``PDx1,y1,x2,y2,...``) stays a single command
on a single line, because breaking it into several commands *would* change
the parsed structure (the parser takes a batched ``PD``'s first pair as the
path's pen-up position).
"""

from __future__ import annotations

from typing import List

from plt_optimizer.core.parser import PLTParser

__all__ = ["format_hpgl_lines"]

# The single tokenizer shared with the parser: a minimal run of
# ``[A-Z][A-Z0-9,.\-:]*`` ending at the first ``;``. Reusing it (rather than
# re-declaring the pattern) keeps this formatter's view of "one command"
# identical to the parser's, which is what makes the transform parse-safe.
_COMMAND_PATTERN = PLTParser.COMMAND_PATTERN

# A bare pen-state command (no coordinates) glues to a following arc command
# (EngraveLab layout: ``PD;AA...`` on one line).
_BARE_PEN_TOKENS = frozenset({"PD;", "PU;"})
_ARC_PREFIXES = ("AA", "AR", "CI")


def format_hpgl_lines(content: str) -> str:
    """Reflow compact HPGL content to one command per line (EngraveLab style).

    Rules (all cosmetic; the parsed document is unchanged):

    - Every command token is placed on its own line.
    - A bare ``PD;``/``PU;`` (no coordinates) is glued to a following arc
      command (``AA``/``AR``/``CI``) on the same line, matching the reference
      tool's ``PD;AA...`` layout.
    - The result ends with exactly one trailing newline. Empty (or
      command-free) input yields an empty string.

    Args:
        content: Raw HPGL PLT text (any existing newlines/whitespace are
            normalized away; only command tokens survive).

    Returns:
        The reflowed content, newline-joined with a single trailing ``\\n``.
        Callers writing to disk open the file with ``newline="\\r\\n"`` to get
        the reference tool's CRLF terminators.
    """
    tokens = [match.group(1) for match in _COMMAND_PATTERN.finditer(content)]
    if not tokens:
        return ""

    lines: List[str] = []
    index = 0
    total = len(tokens)
    while index < total:
        token = tokens[index]
        # Glue a bare pen-state command to the arc that follows it.
        if (
            token in _BARE_PEN_TOKENS
            and index + 1 < total
            and tokens[index + 1].startswith(_ARC_PREFIXES)
        ):
            lines.append(f"{token}{tokens[index + 1]}")
            index += 2
            continue

        lines.append(token)
        index += 1

    return "\n".join(lines) + "\n"
