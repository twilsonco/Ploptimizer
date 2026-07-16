"""PLT-Optimizer generate module.

This module provides the generation pipeline for creating PLT files from
YAML job specifications.
"""

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

__all__ = [
    "DEFAULT_CHAR_SPACING",
    "DEFAULT_LINE_SPACING",
    "DEFAULT_MARGIN",
    "DEFAULT_TEXT_HEIGHT",
    "HoleLocation",
    "HoleSpec",
    "JobSpec",
    "LabelAttributes",
    "LabelSpec",
    "PlateSpec",
    "ResolvedHoleSpec",
    "ResolvedLabel",
    "ResolvedTextLine",
    "TextAttributes",
    "TextLine",
    "calculate_label_dimensions",
    "parse_yaml",
    "resolve_job_spec",
]
