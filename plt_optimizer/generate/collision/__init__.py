"""Text-hole collision avoidance package for the label generation pipeline.

The whole collision system lives here:

- :mod:`plt_optimizer.generate.collision.geometry` - pure predicates: the
  signed circle-vs-AABB gap driving detection, plus the circle-vs-ellipse
  primitives for ``type: round`` work, and the :class:`CollisionResult` record.
- :mod:`plt_optimizer.generate.collision.shapes` - the label-shape predicates
  (round / fully-rounded / rounded-rectangle) shared by the renderer, the
  hole placement, and the collision checks.
- :mod:`plt_optimizer.generate.collision.holes` - drill-hole circle placement
  in the label frame (shared by the emitted ``AA`` arcs and the checks) and
  the native four-arc HPGL emission.
- :mod:`plt_optimizer.generate.collision.detection` - the stroke-aware
  threshold, detection, the two opt-in avoidance phases (hole-margin
  reduction, horizontal compression), diagnostics, and the job-level
  :func:`assert_no_collisions` gate.

The contract is ``AGENTS.md`` beside this file. The renderer
(:mod:`plt_optimizer.generate.label_renderer`) orchestrates the phases and
injects its text renderer into the compression sweep, so this package has
zero runtime imports from ``label_renderer``.
"""

from plt_optimizer.generate.collision.detection import (
    LabelRenderError,
    LineBoundsRender,
    assert_no_collisions,
    collision_line_desc,
    collision_threshold,
    detect_text_hole_collisions,
    format_unresolvable_collision,
    log_collisions,
    resolve_via_compression,
    resolve_via_margin_adjustment,
)
from plt_optimizer.generate.collision.geometry import (
    CollisionResult,
    HoleCenter,
    circle_aabb_gap,
    circle_ellipse_aabb_gap,
    circle_ellipse_gap,
    point_ellipse_signed_distance,
)
from plt_optimizer.generate.collision.holes import (
    hole_circles_local,
    render_holes_hpgl,
    round_corner_hole,
    rounded_rect_corner_hole,
)
from plt_optimizer.generate.collision.shapes import (
    label_ellipse,
    label_is_round,
    rounded_rect_corner,
    rounded_rect_full_ellipse,
    uses_ellipse_boundary,
)

__all__ = [
    "CollisionResult",
    "HoleCenter",
    "LabelRenderError",
    "LineBoundsRender",
    "assert_no_collisions",
    "circle_aabb_gap",
    "circle_ellipse_aabb_gap",
    "circle_ellipse_gap",
    "collision_line_desc",
    "collision_threshold",
    "detect_text_hole_collisions",
    "format_unresolvable_collision",
    "hole_circles_local",
    "label_ellipse",
    "label_is_round",
    "log_collisions",
    "point_ellipse_signed_distance",
    "render_holes_hpgl",
    "resolve_via_compression",
    "resolve_via_margin_adjustment",
    "rounded_rect_corner",
    "rounded_rect_corner_hole",
    "rounded_rect_full_ellipse",
    "uses_ellipse_boundary",
]
