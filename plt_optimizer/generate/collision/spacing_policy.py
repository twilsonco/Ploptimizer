"""Which line-spacing escape a label gets, as a pure decision.

Inter-line spacing is the one collision knob whose useful DIRECTION depends on
the label: the gaps are the only room between the lines, so

- a block with an EVEN number of lines and holes on the left/right edges only
  has a gap sitting on the hole band once the gaps are GROWN (the middle two
  lines step off mid-height and a gap takes their place), while
- a block with holes on the top/bottom edges (the corners included) is cleared
  by PACKING the gaps, which pulls the whole block off the offending edge.

Growing a block with top/bottom holes pushes lines INTO them, and packing an
even-line side-hole block stacks lines onto mid-height where the holes sit, so
each direction is only offered where it can help. This module owns that
decision -- one pure function of the hole axes
(:func:`plt_optimizer.generate.collision.holes.hole_escape_axes`), the line
count, and its parity -- so the renderer gates phases on a named policy instead
of scattering ``len(content) % 2`` tests through the ladder, and the
unresolvable-collision diagnostics can tell the operator which strategy the
label was given.

The contract is ``AGENTS.md`` beside this file.
"""

from __future__ import annotations

from dataclasses import dataclass

from plt_optimizer.generate.collision.holes import hole_escape_axes
from plt_optimizer.generate.resolution import ResolvedLabel

__all__ = ["SpacingPolicy", "select_spacing_policy", "spacing_strategy_summary"]


@dataclass(frozen=True)
class SpacingPolicy:
    """The inter-line spacing escapes available to one label.

    Attributes:
        spread_first: True when the block should try GROWING every gap ahead of
            every other avoidance phase: the holes sit on the left/right edges
            only (so the vertical axis is free and the horizontal ladder is the
            fallback), and the line count is even and at least 2, which is
            exactly the parity that lets a gap replace the middle two lines on
            the hole band.
        pack: True when packing the gaps down to ``min_line_spacing`` is on the
            table, i.e. the holes leave the vertical axis open (top/bottom or
            the corners) and the block has a gap to pack. This mirrors the
            gate of the Phase 2c resolver; it is recorded so reports and
            diagnostics can state the label's strategy.
        line_count: The number of text lines the policy was computed for.
    """

    spread_first: bool
    pack: bool
    line_count: int


def select_spacing_policy(label: ResolvedLabel) -> SpacingPolicy:
    """Select the inter-line spacing escapes for a label.

    Pure function of the label's hole locations and line count; no rendering,
    no I/O, no state.

    Args:
        label: The resolved label to classify.

    Returns:
        The :class:`SpacingPolicy` for the label. A label with no holes (or a
        single line, which has no gap at all) gets both escapes off.
    """
    axes = hole_escape_axes(label)
    line_count = len(label.content)
    multi_line = line_count >= 2
    return SpacingPolicy(
        spread_first=bool(
            multi_line and axes.horizontal and not axes.vertical and line_count % 2 == 0
        ),
        pack=bool(multi_line and axes.vertical),
        line_count=line_count,
    )


def spacing_strategy_summary(label: ResolvedLabel) -> str:
    """Describe, in one line, the spacing strategy selected for a label.

    Used by the unresolvable-collision diagnostics so the operator sees the
    direction the gaps were moved in, and the knob that extends it.

    Args:
        label: The resolved label to describe.

    Returns:
        A single-line clause (no leading dash, no trailing period), or ``""``
        when the label has no inter-line spacing to manage.
    """
    policy = select_spacing_policy(label)
    if policy.line_count < 2:
        return ""
    if policy.spread_first:
        return (
            f"spread first ({policy.line_count} lines, side holes only): the gaps "
            "were grown so a gap sits on the hole band; the spread is capped by "
            "the vertical margins, so increase label height or reduce the "
            "vertical margin to widen it"
        )
    if policy.pack:
        return (
            f"pack to the floor ({policy.line_count} lines, top/bottom/corner "
            "holes): gaps shrink toward min_line_spacing, so lower "
            "min_line_spacing to widen the budget"
        )
    return (
        f"no spacing escape ({policy.line_count} lines): these holes leave no "
        "axis for the gaps to move the block on"
    )
