"""PLT-Optimizer generate module.

This module provides the generation pipeline for creating PLT files from
YAML job specifications.
"""

from plt_optimizer.generate.layout import (
    DEFAULT_PLATE_HEIGHT,
    DEFAULT_PLATE_WIDTH,
    LayoutFitError,
    PackedLabel,
    PackedPlate,
    generate_layout,
    initialize_packer,
    unroll_labels,
)
from plt_optimizer.generate.resolution import (
    DEFAULT_CHAR_SPACING,
    DEFAULT_LINE_SPACING,
    DEFAULT_MARGIN,
    DEFAULT_TEXT_HEIGHT,
    ResolvedHoleSpec,
    ResolvedLabel,
    ResolvedTextLine,
    calculate_label_dimensions,
    resolve_job_spec,
)
from plt_optimizer.generate.schema import (
    HoleLocation,
    HoleSpec,
    JobSpec,
    LabelAttributes,
    LabelSpec,
    PlateSpec,
    TextAttributes,
    TextLine,
    parse_yaml,
)
from plt_optimizer.generate.vectorize import (
    LAYER_BOUNDARY,
    LAYER_HOLES,
    LAYER_TEXT,
    POINTS_PER_INCH,
    export_and_optimize,
    export_to_plt,
    vectorize_plate,
    vectorize_plates,
)

__all__ = [
    "DEFAULT_CHAR_SPACING",
    "DEFAULT_LINE_SPACING",
    "DEFAULT_MARGIN",
    "DEFAULT_PLATE_HEIGHT",
    "DEFAULT_PLATE_WIDTH",
    "DEFAULT_TEXT_HEIGHT",
    "HoleLocation",
    "HoleSpec",
    "JobSpec",
    "LAYER_BOUNDARY",
    "LAYER_HOLES",
    "LAYER_TEXT",
    "LabelAttributes",
    "LabelSpec",
    "LayoutFitError",
    "POINTS_PER_INCH",
    "PackedLabel",
    "PackedPlate",
    "PlateSpec",
    "ResolvedHoleSpec",
    "ResolvedLabel",
    "ResolvedTextLine",
    "TextAttributes",
    "TextLine",
    "calculate_label_dimensions",
    "export_and_optimize",
    "export_to_plt",
    "generate_layout",
    "initialize_packer",
    "parse_yaml",
    "resolve_job_spec",
    "unroll_labels",
    "vectorize_plate",
    "vectorize_plates",
]
