"""Label-shape predicates shared by the renderer and the collision system.

The single source of truth for "is this label round / fully-rounded /
rounded-rectangle" branching: the boundary emission, the ellipse-aware
drill-hole placement, the radial content inset, and the collision checks
all dispatch through these predicates so a sharp rectangular label is
always bit-identical to the pre-round-label pipeline.

Moved verbatim from ``label_renderer.py`` when the collision package was
created; the contract is ``AGENTS.md`` beside this file.
"""

from __future__ import annotations

import math
from typing import Optional

from plt_optimizer.generate.resolution import (
    ResolvedLabel,
    RoundedRectCorner,
    rounded_rect_corner_params,
)
from plt_optimizer.generate.schema import LabelShape


def label_is_round(label: ResolvedLabel) -> bool:
    """True when the label's boundary is an ellipse (``type: round``).

    The single shape predicate the renderer uses to branch onto the ellipse
    path (boundary emission, hole placement, collision distance, radial
    content inset). Rectangular labels never enter it, so their geometry is
    bit-identical.

    Args:
        label: The resolved label.

    Returns:
        ``True`` for ``shape_type == "round"``.
    """
    return label.shape_type == LabelShape.ROUND.value


def label_ellipse(
    label: ResolvedLabel,
) -> tuple[float, float, float, float]:
    """The label's elliptical boundary in the label-local y-up frame.

    The boundary rectangle is drawn at the label edge, so the ellipse is
    centred at ``(width / 2, height / 2)`` with semi-axes ``width / 2`` and
    ``height / 2`` -- the same frame the drill holes and text bounds use.

    Args:
        label: The resolved (round) label.

    Returns:
        ``(center_x, center_y, semi_a, semi_b)`` in inches.
    """
    return (label.width / 2.0, label.height / 2.0, label.width / 2.0, label.height / 2.0)


def rounded_rect_full_ellipse(label: ResolvedLabel) -> bool:
    """True when the rounding closes the border into the exact ellipse/circle.

    ``rounded_rectangle_fraction == 1.0`` with
    ``rounded_rectangle_proportional`` true -- or on a square, where the two
    modes coincide -- makes the corner patches meet at every edge midpoint
    and the exponent land on ``n = 2``: the boundary is exactly the
    ``type: round`` ellipse (a circle on a square). Those labels route to the
    shipped ellipse path (boundary polygon, corner holes, the relief's lower
    arc), so their engraved geometry matches a ``type: round`` label of the
    same box; only the packing stays rectangular (the nominal box is exact
    for the ellipse too).

    Args:
        label: The resolved label.

    Returns:
        ``True`` for a fully-rounded rectangular label.
    """
    if label_is_round(label):
        return False
    fraction = label.rounded_rectangle_fraction
    if fraction <= 0.0 or not math.isclose(fraction, 1.0):
        return False
    return label.rounded_rectangle_proportional or math.isclose(label.width, label.height)


def uses_ellipse_boundary(label: ResolvedLabel) -> bool:
    """True when the boundary is the shipped ellipse contour.

    ``type: round`` labels and the fully-rounded rounded-rectangle limit
    (see :func:`rounded_rect_full_ellipse`) share the ellipse boundary
    emission, the ellipse-aware drill-hole placement and the relief's lower
    arc. Packing is NOT dispatched here: a
    fully-rounded rectangular label keeps packing in its nominal box (the
    packer's ``shape_type`` check is untouched).

    Args:
        label: The resolved label.

    Returns:
        ``True`` when the renderer draws the ellipse boundary.
    """
    return label_is_round(label) or rounded_rect_full_ellipse(label)


def rounded_rect_corner(label: ResolvedLabel) -> Optional[RoundedRectCorner]:
    """The label's rounded-rectangle corner parameters, if rounded.

    The single predicate the renderer uses to branch onto the rounded-
    rectangle border path (boundary emission, corner drill-hole placement,
    corner-aware content inset, the descender relief's bottom border).
    ``type: round`` labels and the fully-rounded ellipse limit return
    ``None`` -- the shipped ellipse path owns those boundaries (see
    :func:`rounded_rect_full_ellipse`). Sharp labels
    (``rounded_rectangle_fraction == 0.0``, the default) also return
    ``None``, so their geometry stays bit-identical.

    Args:
        label: The resolved label.

    Returns:
        The corner parameters, or ``None`` for a sharp (or round-shaped /
        fully-rounded) label.
    """
    if label_is_round(label) or rounded_rect_full_ellipse(label):
        return None
    if label.rounded_rectangle_fraction <= 0.0:
        return None
    return rounded_rect_corner_params(
        label.width,
        label.height,
        label.rounded_rectangle_fraction,
        label.rounded_rectangle_proportional,
    )
