"""Plate-space packing package for the label generation pipeline.

Two adjacent bin-packing engines share this package:

- :mod:`plt_optimizer.generate.packing.layout` - the ``rectpack``-based
  engine for rectangular labels (bounds-aware multi-heuristic packing,
  rotation, fill order, plate clearances, material partitioning).
- :mod:`plt_optimizer.generate.packing.ellipse_packer` - the shapely-based
  grid-search engine for ``type: round`` labels (the ellipse boundary, not
  its bounding box, is the collision object).

The contract for both lives in ``AGENTS.md`` beside this file. Plate-frame
TSP *routing* is not packing - it stays in
:mod:`plt_optimizer.generate.plate_optimizer`.
"""

from plt_optimizer.generate.packing.ellipse_packer import (
    DEFAULT_CLEARANCE_IN,
    EllipsePacker,
    EllipsePackingResult,
    EllipseSpec,
    ScrapDirection,
    ellipse_polygon,
    pack_ellipses,
)
from plt_optimizer.generate.packing.layout import (
    DEFAULT_PLATE_HEIGHT,
    DEFAULT_PLATE_WIDTH,
    DEFAULT_ROUND_CLEARANCE_IN,
    PACK_CONFIGS,
    LayoutFitError,
    PackedLabel,
    PackedPlate,
    generate_layout,
    generate_layout_with_bounds,
    unroll_labels,
)

__all__ = [
    "DEFAULT_CLEARANCE_IN",
    "DEFAULT_PLATE_HEIGHT",
    "DEFAULT_PLATE_WIDTH",
    "DEFAULT_ROUND_CLEARANCE_IN",
    "PACK_CONFIGS",
    "EllipsePacker",
    "EllipsePackingResult",
    "EllipseSpec",
    "LayoutFitError",
    "PackedLabel",
    "PackedPlate",
    "ScrapDirection",
    "ellipse_polygon",
    "generate_layout",
    "generate_layout_with_bounds",
    "pack_ellipses",
    "unroll_labels",
]
