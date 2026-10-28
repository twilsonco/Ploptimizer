"""JSON-driven default values for YAML job specifications.

This module defines ``job-config.json`` — a shop-level companion to
``tools.json`` that supplies the *top-layer* default values the generate
pipeline would otherwise fall back to as hard-coded constants. A shop can
maintain one config per engraver / toolset and select it per run via the
``generate`` CLI's ``--job-config`` flag (default ``job-config.json``).

Semantics:

- Every configured default is applied at the **top-most layer** of the
  cascade: cascading attributes (margins, compression, collision
  distances, ...) are injected as *job-level* values, so the existing
  label -> job cascade works unchanged and labels still override them.
- A job spec that declares the universal ``margin`` (at the job level, or
  on any label) suppresses the configured ``h_margin`` / ``v_margin``
  injection: ``margin`` is the spec's intent for *every* edge, and the
  cascade checks ``job.h_margin`` / ``job.v_margin`` *before* ``margin``,
  so an injected axis-specific default would silently outrank the spec's
  margin on that axis.
- ``plate_width`` / ``plate_height`` are *plate-layer* defaults: they fill
  plate entries that omit ``width`` / ``height`` and drive the unbounded
  (auto-allocated) bin size. They deliberately never fill the job-level
  label ``width`` / ``height`` (that would defeat label auto-sizing).
- ``left_clearance`` / ``top_clearance`` are injected at the **job layer**
  (like the other cascading fields): the ``JobSpec`` cascade then applies
  them to plate entries that omit the matching clearance, and the
  unbounded (auto-allocated) bins shift by the same pair. A job spec may
  omit ``plates:`` entirely: packing then runs in unbounded mode on
  config-sized default sheets, overflowing onto as many as needed.
- **Required-when-unconfigured:** the fields in
  :data:`REQUIRED_WHEN_UNCONFIGURED` must be provided either by this config
  or by the job spec (job level, or every label individually). When a
  config is in play and neither source supplies one of them, parsing fails
  with a clear error instead of silently applying a hard-coded default.
- A field that is missing from the JSON, explicitly ``null``, or whose
  config file is absent simply has no configured default (the required
  rule above then applies; fields outside the required set keep their
  in-code fallbacks).
- :func:`load_job_config` is the config's **full** gate: the
  :class:`JobDefaults` model rejects unknown keys and out-of-range values,
  canonicalizes ``font`` against the font registry, and validates every
  ``tool_options`` metadata entry (see
  :func:`validate_tool_options_metadata`) so a malformed shop config fails
  at load with all problems listed, instead of crashing mid-export.

Example:
    >>> from pathlib import Path
    >>> from plt_optimizer.generate.schema import parse_yaml
    >>> job = parse_yaml(
    ...     "tests_deps/test123_spec.yaml",
    ...     job_config_path=Path("job-config.json"),
    ... )
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from plt_optimizer.generate.font_registry import normalize_font_name
from plt_optimizer.generate.preview_colors import normalize_preview_background_color
from plt_optimizer.generate.schema import (
    CounterStyle,
    HoleSpec,
    LabelShape,
    LayoutMode,
    TextHAlignment,
    _validate_replacement_delimiter_value,
)

logger = logging.getLogger(__name__)

# Job-spec fields that lose their silent hard-coded fallback once a job
# config is in play: the value must come from the config or from the job
# spec itself (job level, or declared on every label). ``plate_width`` /
# ``plate_height`` are enforced separately (plate entries are validated
# structurally; unbounded auto-allocation needs the config values).
REQUIRED_WHEN_UNCONFIGURED: frozenset[str] = frozenset(
    {
        "max_h_compress",
        "hole_margin",
        "min_hole_margin",
        "hole_text_collision_distance",
    }
)


class JobConfigError(ValueError):
    """Raised when a job-config JSON file is invalid.

    Subclasses :class:`ValueError` so callers that already guard the
    pipeline with ``except ValueError`` keep working unchanged.
    """


#: Valid ``tool_options`` entry ``type`` values (mirrors the dispatch in
#: :func:`plt_optimizer.generate.tool_options.generate_tool_option_headers`).
TOOL_OPTION_TYPES: tuple[str, ...] = ("bool", "int", "float")

#: The two dual-default layer names a numeric/bool option may split between.
TOOL_OPTION_DEFAULT_LAYERS: tuple[str, ...] = ("text", "borders_holes")

#: Option keys are the job-spec ``tool_options`` lookup names, so they follow
#: the same snake_case convention the spec uses for every other field.
_TOOL_OPTION_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


def _is_number(value: Any) -> bool:
    """Return True for a real numeric value (``bool`` is excluded).

    ``isinstance(True, int)`` is True in Python, which would let a boolean
    masquerade as a numeric default/header value, so bools are rejected here.

    Args:
        value: The candidate value.

    Returns:
        True when ``value`` is an ``int`` or ``float`` and not a ``bool``.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_tool_option_entry(key: str, meta: Any) -> list[str]:
    """Collect every problem with one ``tool_options`` metadata entry.

    Args:
        key: The option key being validated.
        meta: The entry value from the config.

    Returns:
        Human-readable problems (empty when the entry is sound).
    """
    prefix = f"tool_options.{key}"
    if not isinstance(meta, dict):
        return [f"{prefix}: must be an object, got {type(meta).__name__}"]

    problems: list[str] = []

    command = meta.get("command")
    if not isinstance(command, str) or not command.strip():
        problems.append(
            f"{prefix}.command: required non-empty string (the HPGL prefix); "
            f"without it the emitted header has no command and the PLT is corrupt"
        )
    elif ";" in command:
        problems.append(f"{prefix}.command: must not contain ';' (it ends the HPGL statement)")

    param_type = meta.get("type")
    if param_type not in TOOL_OPTION_TYPES:
        problems.append(
            f"{prefix}.type: must be one of {', '.join(TOOL_OPTION_TYPES)}, got "
            f"{param_type!r}; an unknown type silently drops the option from the header"
        )
        return problems

    default = meta.get("default")
    if default is None:
        problems.append(f"{prefix}.default: required (the value used when a spec omits the option)")
    elif isinstance(default, dict):
        # Dual default: one value per PLT type.
        unexpected = sorted(set(default) - set(TOOL_OPTION_DEFAULT_LAYERS))
        missing = sorted(set(TOOL_OPTION_DEFAULT_LAYERS) - set(default))
        if unexpected:
            problems.append(
                f"{prefix}.default: unknown layer(s) {', '.join(unexpected)}; "
                f"expected only {', '.join(TOOL_OPTION_DEFAULT_LAYERS)}"
            )
        if missing:
            problems.append(
                f"{prefix}.default: missing layer(s) {', '.join(missing)}; a dual "
                f"default must cover {', '.join(TOOL_OPTION_DEFAULT_LAYERS)}"
            )
        for layer in TOOL_OPTION_DEFAULT_LAYERS:
            value = default.get(layer)
            if value is None:
                continue
            if param_type == "bool" and not isinstance(value, bool):
                problems.append(f"{prefix}.default.{layer}: must be a boolean for type 'bool'")
            elif param_type != "bool" and not _is_number(value):
                problems.append(
                    f"{prefix}.default.{layer}: must be a number for type '{param_type}'"
                )
    elif param_type == "bool":
        if not isinstance(default, bool):
            problems.append(f"{prefix}.default: must be a boolean for type 'bool'")
    elif not _is_number(default):
        problems.append(f"{prefix}.default: must be a number for type '{param_type}'")

    if param_type == "bool":
        # The header formatters ignore min/max for booleans; no bounds check.
        return problems

    bounds: dict[str, Any] = {}
    for bound in ("min", "max"):
        value = meta.get(bound)
        bounds[bound] = value
        if value is None:
            problems.append(f"{prefix}.{bound}: required for type '{param_type}' (the clamp range)")
        elif not _is_number(value):
            problems.append(f"{prefix}.{bound}: must be a number for type '{param_type}'")

    low, high = bounds["min"], bounds["max"]
    if _is_number(low) and _is_number(high) and low > high:
        problems.append(
            f"{prefix}.min: {low} is greater than max {high}; every value would clamp to {low}"
        )
    return problems


def validate_tool_options_metadata(value: Any) -> None:
    """Validate a config ``tool_options`` metadata mapping.

    :class:`JobDefaults.tool_options` is intentionally typed
    ``dict[str, Any]`` (its keys are engraver option names, not schema
    fields), which leaves the *values* unchecked. This is the semantic gate:
    it rejects the shapes that crash export, emit a header with no command,
    or silently drop a cutter option.

    Args:
        value: The configured mapping (any shape), or ``None`` (unset).

    Raises:
        ValueError: If the mapping is not an object, an option key is not
            snake_case, or any entry fails
            :func:`_validate_tool_option_entry`. Every problem found is
            reported in one message.
    """
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ValueError(f"Job config tool_options must be an object, got {type(value).__name__}")

    problems: list[str] = []
    for key in sorted(value, key=str):
        label = key if isinstance(key, str) else repr(key)
        if not isinstance(key, str) or not _TOOL_OPTION_KEY_RE.match(key):
            problems.append(
                f"tool_options.{label}: key must be a snake_case name "
                f"(it is the job-spec tool_options lookup key)"
            )
            continue
        problems.extend(_validate_tool_option_entry(key, value[key]))

    if problems:
        detail = "\n".join(f"  - {problem}" for problem in problems)
        raise ValueError(f"Invalid job config tool_options ({len(problems)} problem(s)):\n{detail}")


class JobDefaults(BaseModel):
    """Configured default values from ``job-config.json``.

    Every field is optional; ``None`` (missing or explicit JSON ``null``)
    means "no configured default". Field names and validation mirror the
    corresponding :class:`~plt_optimizer.generate.schema.JobSpec` /
    :class:`~plt_optimizer.generate.schema.PlateSpec` fields. Unknown keys
    are rejected so typos in a shop config fail loudly.

    Attributes:
        text_height: Default font height in inches.
        font: Default font name selecting the glyph outlines (PLT-extracted
            catalog ``font_name`` key or ``fonts/`` TTF basename,
            case-insensitive). Unset falls back to the schema default
            (``ReliefSingleLineCAD-Regular``).
        character_spacing: Default extra spacing between characters (in
            inches). Unset falls back to the per-cutter derived default.
        line_spacing: Default extra spacing between text lines in inches.
        margin: Default label margin in inches. A spec-declared margin
            (job level, or on any label) suppresses the configured
            ``h_margin`` / ``v_margin`` injection.
        h_margin: Default horizontal (left/right) margin in inches; falls
            back to margin if unset. Not injected when the spec declares
            a margin.
        v_margin: Default vertical (top/bottom) margin in inches; falls
            back to margin if unset. Not injected when the spec declares
            a margin.
        hole_margin: Default hole margin in inches.
        min_hole_margin: Default collision-avoidance floor for the hole
            margin in inches.
        hole_text_collision_distance: Default engraved-stroke air gap in
            inches between text and drill-hole strokes.
        cutter_buffer_text: Default kerf buffer in inches added to a text
            line's cutter diameter to get the width it engraves (a
            roughly 45-degree-flank V-bit cuts wider than nominal). The
            effective width drives the geometry and the PDF proof stroke
            widths; the tool, the pen map and the PLT file names stay
            physical. Unset falls back to 0.0 (legacy: engraved = nominal).
        cutter_buffer_routing: Default kerf buffer in inches added to the
            boundary/drill-hole cutter diameter, the structural sibling of
            ``cutter_buffer_text``. Unset falls back to 0.0.
        max_h_compress: Default maximum horizontal compression fraction
            in ``[0.0, 1.0]``.
        space_width_fraction: Default space advance fraction for
            PLT-extracted fonts (space = fraction * text_height +
            character_spacing). Unset falls back to 0.3.
        min_glyph_width_fraction: Default global minimum glyph advance
            width as a fraction of the rendered cap height for
            PLT-extracted fonts. The profile-envelope kerning
            floors each pair's advance at this fraction of the cap height,
            capped by the left
            glyph's own width, so zero-width glyphs still reserve real air.
            Unset falls back to 0.0
            (pure profile-envelope kerning).
        kerning_window_fraction: Default kerning window for PLT-extracted
            fonts, as a fraction of the rendered text height in
            ``[0.0, 1.0]``. Unset falls back to 0.05; 0.0 reproduces the
            historical same-height maximum-penetration kerning.
        kerning_penetration_scale: Default multiplier on the detected
            windowed penetration for PLT-extracted fonts. Unset falls
            back to 1.0 (geometric).
        kerning_recession_scale: Default multiplier on a *recessed*
            (negative) detected penetration for PLT-extracted fonts.
            Unset falls back to 1.0 (geometric); 0.0 makes a recessed
            pair advance by nothing beyond the min_glyph_width_fraction
            floor.
        kerning_min_gap_fraction: Default extra air added to every
            kerned pair advance for PLT-extracted fonts, as a fraction of
            the rendered cap height. Unset falls back
            to 0.0.
        fallback_advance_fraction: Default multiplier on the
            bounding-box-width fallback advance for PLT-extracted pairs
            without overlapping height. Unset falls back to 1.0.
        kern_pos_string: Default manual-kern positive marker string: every
            occurrence in a text line is stripped (it never engraves) and
            replaced by a forward cursor shift of ``kern_width_fraction``
            of the cap height. Unset (``null``) disables the positive
            marker.
        kern_neg_string: Default manual-kern negative marker string: every
            occurrence is stripped and replaced by a backward cursor shift
            of ``kern_width_fraction`` of the cap height. Unset (``null``)
            disables the negative marker.
        kern_width_fraction: Default manual-kern width as a fraction of the
            rendered cap height (capline - baseline). Unset falls back to
            0.1.
        text_h_alignment: Default horizontal text alignment.
        holes: Default drill-hole list (injected at the job layer; group
            locations expand exactly like spec-provided holes).
        hole_diameter: Default drill-hole diameter in inches, applied to
            hole entries (config- or spec-provided) that omit
            ``diameter``.
        allow_rotation: Default bin-packing rotation permission.
        cutter_downsize: Default compression-driven cutter reduction
            permission. Unset falls back to True.
        max_cutter_downsizes: Default ceiling on successive cutter
            downsizings per text line. Unset falls back to 1; 0 disables
            the mechanism.
        text_chunk_mode: Default plate-space text optimization granularity.
        layout: Default plate fill order.
        replacement_text_delimiter: Default replacement-file item delimiter
            (job layer; cascades job -> plate -> label). Unset falls back to
            the code default ``";"``. Injected only when the spec declares a
            replacement text file at some tier.
        replacement_text_file_comment_string: Default replacement-file
            comment marker (job layer; cascades job -> plate -> label).
            Unset falls back to the code default ``"# "``; ``""`` disables
            comment handling (every non-blank line is raw data; blank lines
            are still dropped). Injected only when the spec declares a
            replacement text file at some tier.
        type: Default label boundary shape ('rect' historical, 'round'
            ellipse/circle). Labels that omit it inherit the job value.
        plate_width: Default usable plate width in inches (plate layer).
        plate_height: Default usable plate height in inches (plate layer).
        left_clearance: Default plate left-edge clearance in inches
            (injected at the job layer; cascades onto plates that omit it).
        top_clearance: Default plate top-edge clearance in inches
            (injected at the job layer; cascades onto plates that omit it).
        auto_line_spacing_interline_to_top_bottom_ratio: Ratio controlling
            inter-line spacing relative to top/bottom margins when auto line
            spacing is enabled.
        auto_descending_character_adjustment: Default automatic
            descending-character relief permission. Unset falls back to True.
        descending_character_v_scale: Default deepest vertical downscale for a
            triggering descending glyph. Unset falls back to 0.5.
        descending_character_raise: Default deepest raise for a triggering
            descending glyph, as a fraction of its descender depth. Unset
            falls back to 0.7.
        descending_character_clearance: Default extra engraved-stroke air gap
            for descending-character relief, as a unitless factor of the stroke
            floor (required gap = floor * (1 + clearance)). Unset falls back
            to 0.0.
        descending_character_reciprocal_scale: Default deepest top-down
            downscale permitted for the colliding glyphs on the following line,
            in [0.2, 1.0] (1.0 = reciprocal downscaling forbidden). Unset falls
            back to 0.5.
        hole_adjust_for_cutter: Default cutter-compensation permission for
            drill holes. When true, the emitted hole circle shrinks by the
            boundary/hole cutter diameter so the engraved hole measures the
            specified diameter. Unset falls back to True.
        tool_options: Engraver tool options (header commands) metadata with
            HPGL command prefixes, types, units, dual defaults, and bounds.
        preview_background_color: Page/plot background color (hex string)
            of the combined ``all_*.pdf`` proof previews; drill holes punch
            the same color. Unset falls back to ``#D3D3D3`` (light grey).
            Display-only: never injected into the job spec and never
            affects the emitted PLTs.
    """

    model_config = ConfigDict(extra="forbid")

    description: Optional[str] = Field(
        default=None,
        description="Free-form comment field (mirrors tools.json); not a default.",
    )
    text_height: Optional[float] = Field(
        default=None, description="Default font height in inches (job layer)."
    )
    font: Optional[str] = Field(
        default=None,
        description=(
            "Default font name (job layer): a PLT-extracted font key or a "
            "fonts/ TTF basename, case-insensitive. Canonicalized and "
            "validated at config load (an unknown name fails here, not in "
            "the job spec); unset falls back to the schema default font."
        ),
    )
    character_spacing: Optional[float] = Field(
        default=None, description="Default extra spacing between characters in inches (job layer)."
    )
    line_spacing: Optional[float] = Field(
        default=None, description="Default extra spacing between text lines in inches (job layer)."
    )
    margin: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default label margin in inches (job layer). A spec-declared "
            "margin (job level, or on any label) suppresses the configured "
            "h_margin/v_margin injection."
        ),
    )
    h_margin: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default horizontal margin (left/right) in inches; falls back to "
            "margin if unset (job layer). Not injected when the spec declares "
            "a margin (job level, or on any label)."
        ),
    )
    v_margin: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default vertical margin (top/bottom) in inches; falls back to "
            "margin if unset (job layer). Not injected when the spec declares "
            "a margin (job level, or on any label)."
        ),
    )
    hole_margin: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Default hole margin in inches (job layer; required-when-unconfigured).",
    )
    min_hole_margin: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default collision-avoidance floor for the hole margin in inches "
            "(job layer; required-when-unconfigured)."
        ),
    )
    hole_text_collision_distance: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default engraved-stroke air gap in inches between text and "
            "drill-hole strokes (job layer; required-when-unconfigured)."
        ),
    )
    cutter_buffer_text: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default kerf buffer in inches added to a text line's cutter "
            "diameter to get the width it actually engraves (a roughly "
            "45-degree-flank V-bit cuts wider than its nominal diameter). "
            "The effective width drives the geometry (kerning clearance, "
            "collision stroke floor, compression margin) and the PDF proof "
            "stroke widths, and the toolpath height compensates so engraved "
            "text stays text_height tall; the tool, the pen map and the PLT "
            "file names stay physical. Cascades line -> label -> job; unset "
            "falls back to 0.0 (engraved = nominal)."
        ),
    )
    cutter_buffer_routing: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default kerf buffer in inches added to the boundary/drill-hole "
            "cutter diameter to get the width it actually engraves (the "
            "structural sibling of cutter_buffer_text). The effective width "
            "drives the cutter-compensated hole radius, the hole margin edge "
            "offset, the collision stroke floor, the round-label packing kerf "
            "and the PDF proof stroke widths; the *_bh_* file name keeps the "
            "physical tool. Cascades line -> label -> job; unset falls back "
            "to 0.0."
        ),
    )
    max_h_compress: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Default maximum horizontal compression fraction in [0.0, 1.0] "
            "(job layer; required-when-unconfigured)."
        ),
    )
    text_h_alignment: Optional[TextHAlignment] = Field(
        default=None, description="Default horizontal text alignment (job layer)."
    )
    space_width_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default space advance fraction for PLT-extracted fonts (job "
            "layer): a space advances fraction * text_height + "
            "character_spacing. Unset falls back to 0.3."
        ),
    )
    min_glyph_width_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default global minimum glyph advance width as a fraction of "
            "the rendered cap height (capline - baseline) for "
            "PLT-extracted fonts (job layer); clamps the profile-envelope "
            "kerning for zero-width glyphs. Unset falls back to 0.0."
        ),
    )
    kerning_window_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Default kerning window for PLT-extracted fonts (job layer) as "
            "a fraction of the rendered text height in [0.0, 1.0]; widens "
            "the envelope comparison so staggered pokes widen the "
            "advance. Unset falls back to 0.05."
        ),
    )
    kerning_penetration_scale: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default kerning penetration multiplier for PLT-extracted "
            "fonts (job layer); over-kerns tight pairs proportionally. "
            "Unset falls back to 1.0."
        ),
    )
    kerning_recession_scale: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default kerning recession multiplier for PLT-extracted fonts "
            "(job layer); scales a negative (recessed) detected "
            "penetration. Unset falls back to 1.0; 0.0 makes a recessed "
            "pair advance by nothing beyond the min_glyph_width_fraction "
            "floor."
        ),
    )
    kerning_min_gap_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default extra kerning air added to every kerned "
            "pair advance for PLT-extracted fonts (job layer), as a "
            "fraction of the rendered cap height (capline - baseline). "
            "Unset falls back to 0.0."
        ),
    )
    fallback_advance_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default fallback-advance multiplier for PLT-extracted glyph "
            "pairs without overlapping height (job layer). Unset falls "
            "back to 1.0."
        ),
    )
    kern_pos_string: Optional[str] = Field(
        default=None,
        description=(
            "Default manual-kern positive marker string (job layer): every "
            "occurrence in a text line is stripped and replaced by a "
            "forward cursor shift of kern_width_fraction of the cap height. "
            "Validated like the cascading field (non-empty, no newline, not "
            "markup-shaped). Unset (null) disables the positive marker."
        ),
    )
    kern_neg_string: Optional[str] = Field(
        default=None,
        description=(
            "Default manual-kern negative marker string (job layer): every "
            "occurrence in a text line is stripped and replaced by a "
            "backward cursor shift of kern_width_fraction of the cap height. "
            "Validated like the cascading field (non-empty, no newline, not "
            "markup-shaped). Unset (null) disables the negative marker."
        ),
    )
    kern_width_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default manual-kern width as a fraction of the rendered cap "
            "height (job layer): one marker shifts the cursor by "
            "fraction * cap height (positive string forward, negative "
            "backward). Unset falls back to 0.1."
        ),
    )
    sc_scale: Optional[float] = Field(
        default=None,
        ge=0.2,
        le=1.0,
        description=(
            "Default small-caps downscale applied to ASCII lowercase a-z "
            "by a ' SC' font variant (job layer; the lowercase character "
            "renders as the base font's uppercase glyph uniformly shrunk "
            "to this fraction of the cap height). Unset falls back to 0.7."
        ),
    )
    supersubscript_downscale: Optional[float] = Field(
        default=None,
        ge=0.2,
        le=1.0,
        description=(
            "Default downscale applied to a bare <sup>/<sub> markup run "
            "(job layer; the run's glyphs engrave uniformly shrunk to this "
            "fraction and the baseline shift derives from it, centering the "
            "shrunken glyph on its line -- superscript rises 1 - d/2 cap "
            "heights, subscript drops d/2). The argumented "
            "forms <sup:0.3>/<sub:-0.2> set the shift explicitly and apply "
            "no scaling. Unset falls back to 0.5."
        ),
    )
    html_brackets: Optional[str] = Field(
        default=None,
        description=(
            "Default inline-markup delimiter pair (job layer): an "
            "even-length string whose first half is the left delimiter and "
            "second half the right ('<>' -> '<' + '>'). Unset falls back "
            "to '<>'."
        ),
    )
    italic_angle: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=45.0,
        description=(
            "Default italic slant angle in degrees for <i> markup (job "
            "layer; PLT glyphs and TTF glyphs without a native italic face "
            "are sheared about the baseline). Bound to [0, 45]; 0 means "
            "upright. Unset falls back to 12."
        ),
    )
    underline_offset_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default underline (<u>) extra air as a fraction of the "
            "rendered cap height (capline - baseline; job layer): the "
            "underline centerline sits cutter diameter + fraction * cap "
            "height below "
            "the baseline (the engraved underline's top edge floats exactly "
            "that air below the engraved baseline stroke). The break test "
            "is strictly horizontal, so a glyph breaks the underline where its "
            "ink crosses the underline's centerline; raising it "
            "lifts the underline clear of a glyph and stops it breaking. "
            "Unset "
            "falls back to 0.0."
        ),
    )
    underline_break_clearance_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default underline (<u>) break clearance as a fraction of the "
            "rendered cap height (capline - baseline; job layer): "
            "each cut is padded horizontally by 0.5 * cutter + fraction * "
            "cap height (the half cutter is the glyph stroke's own "
            "half-width), so the engraved air between a descending glyph's "
            "stroke and the underline's cut edge is exactly this value. 0.0 "
            "= the cut sits at the ink. Unset falls back to 0.0."
        ),
    )
    underline_break_spaces: Optional[bool] = Field(
        default=None,
        description=(
            "Default underline (<u>) break-at-spaces flag (job layer): false "
            "(default) draws one continuous underline across the whole "
            "underlined run (spaces included, broken only by descending "
            "glyphs); true ends the underline at each space so every word "
            "gets its own underline and the inter-word space stays bare. "
            "Unset falls back to false."
        ),
    )
    strikethrough_offset_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default strikethrough (<s>) height as a fraction of the "
            "rendered cap height (capline - baseline; job layer): the "
            "stroke (cut with the line's own text cutter) "
            "sits fraction * cap height above the baseline, so 0.0 puts it "
            "on the baseline and 1.0 on the capline. Unlike the underline "
            "the stroke is never broken -- a "
            "descender punches through it. Unset (null) centres the stroke on "
            "the cap-band midline (half the rendered cap height)."
        ),
    )
    strikethrough_break_spaces: Optional[bool] = Field(
        default=None,
        description=(
            "Default strikethrough (<s>) break-at-spaces flag (job layer): "
            "false (default) draws one continuous stroke across the whole "
            "struck run (spaces included); true ends the stroke at each "
            "space so every word gets its own strike and the inter-word "
            "space stays bare. Unset falls back to false."
        ),
    )
    v_center_chars: Optional[str] = Field(
        default=None,
        description=(
            "Default auto-vertical-centred character set (job layer): every "
            "text character occurring in this string is raised or lowered so "
            "its own ink is centred between the baseline and the capline "
            "(the cap-band midline) -- the characters a font typically draws "
            "too high (hyphen, colon, en/em dash, '=', '<', '>'). The inline "
            "<vc>...</vc> tag applies the same centering to any characters it "
            "wraps. Empty string '' disables the auto-apply. Unset falls back "
            "to '' (auto-apply off); the shop config ships '-:–—=<>'."
        ),
    )
    target_boldness_factor: Optional[float] = Field(
        default=None,
        ge=0.05,
        le=0.30,
        description=(
            "Default multi-stroke target engraved stroke width as a fraction "
            "of text height (job layer; sizes the automatic cutter of "
            "multi-stroke/bold PLT fonts so the engraved passes total this "
            "width; inert on single-stroke fonts, TTF fonts, and lines with "
            "an explicit cutter_size). Unset falls back to 0.14."
        ),
    )
    multistroke_overlap_ratio: Optional[float] = Field(
        default=None,
        ge=0.1,
        le=0.5,
        description=(
            "Default multi-stroke minimum pass overlap ratio (job layer; the "
            "automatic cutter of a multi-stroke/bold PLT font is never "
            "smaller than pass_spacing / (1 - ratio), so adjacent engraved "
            "passes always overlap and no unengraved stripe appears). Unset "
            "falls back to 0.25."
        ),
    )
    bold_boldness_multiplier: Optional[float] = Field(
        default=None,
        ge=1.0,
        le=4.0,
        description=(
            "Default bold markup (<b>) boldness multiplier (job layer): the "
            "bold runs of a multi-stroke (bold) PLT font re-run the cutter "
            "ladder at target_boldness_factor * this multiplier (clamped to "
            "0.30). Inert on single-stroke PLT and TTF fonts, where <b> "
            "steps to the next larger inventory cutter. Unset falls back "
            "to 1.5."
        ),
    )
    holes: Optional[list[HoleSpec]] = Field(
        default=None,
        description="Default drill-hole list (job layer; group locations expand like spec holes).",
    )
    hole_diameter: Optional[float] = Field(
        default=None,
        gt=0.0,
        description=(
            "Default drill-hole diameter in inches, applied to hole entries "
            "(config- or spec-provided) that omit 'diameter'."
        ),
    )
    allow_rotation: Optional[bool] = Field(
        default=None, description="Default bin-packing rotation permission (job layer)."
    )
    cutter_downsize: Optional[bool] = Field(
        default=None,
        description=(
            "Default compression-driven cutter reduction permission (job "
            "layer; cascades line -> label -> job)."
        ),
    )
    max_cutter_downsizes: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "Default ceiling on successive cutter downsizings per text line "
            "(job layer; 0 disables the mechanism, 1 = at most one size down)."
        ),
    )
    cutter_downsize_global: Optional[bool] = Field(
        default=None,
        description=(
            "Default per-label sharing of compression-driven cutter downsizes "
            "(job layer; a trigger line's swap applies to every eligible line "
            "of the same text_height in the same label)."
        ),
    )
    cutter_downsize_global_job_wide: Optional[bool] = Field(
        default=None,
        description=(
            "Default job-wide scope of the shared compression-driven cutter "
            "downsizes (job layer; true widens a trigger line's sharing group "
            "from its label to every eligible line of the same text_height "
            "across the whole job, so one text size engraves with one tool)."
        ),
    )
    h_compress_global: Optional[bool] = Field(
        default=None,
        description=(
            "Default per-label sharing of horizontal compression (job layer; "
            "a trigger line's compression scale applies to every eligible line "
            "of the same text_height in the same label)."
        ),
    )
    h_compress_global_same_height_only: Optional[bool] = Field(
        default=None,
        description=(
            "Default grouping scope of the shared horizontal compression (job "
            "layer; true shares the compression only between the lines of the "
            "same text_height, false shares it across every eligible line of "
            "the label). Unset falls back to True."
        ),
    )
    arc_preserve_compression: Optional[bool] = Field(
        default=None,
        description=(
            "Default arc-preserving non-uniform text transforms (job layer; "
            "true refits each PLT glyph arc's ellipse image with circular AA "
            "arcs instead of polygonizing, so compressed/italic/clamped lines "
            "engrave faster). Unset falls back to True."
        ),
    )
    optimize_line_content: Optional[bool] = Field(
        default=None,
        description=(
            "Default word-level line-content reflow permission (job layer; "
            "consecutive enabled text lines redistribute their words to "
            "equalize rendered width, minimizing horizontal compression). "
            "Unset falls back to False."
        ),
    )
    use_baseline_spacing: Optional[bool] = Field(
        default=None,
        description=(
            "Default baseline-to-baseline line spacing permission (job layer; "
            "cascades label -> job, never text lines). True stacks a label's "
            "lines by baseline pitch so descenders hang into the gap and every "
            "gap reads as one uniform visual spacing; false stacks by rendered "
            "ink box. Unset falls back to true."
        ),
    )
    v_center_by_content: Optional[bool] = Field(
        default=None,
        description=(
            "Default vertical centering mode (job layer; cascades label -> job, "
            "never text lines). False (the default) centers a label's text "
            "block by its baseline-to-capline band, so the visible text is "
            "optically centered and descenders hang into the bottom margin for "
            "the descending-character relief to handle against the bottom "
            "border; true centers by rendered ink (historical), which charges "
            "the deepest descender to the block and makes lines with descenders "
            "ride high. Band centering is active only when "
            "auto_descending_character_adjustment is enabled. Unset falls back "
            "to false."
        ),
    )
    auto_descending_character_adjustment: Optional[bool] = Field(
        default=None,
        description=(
            "Default automatic descending-character relief permission (job "
            "layer; cascades label -> job, never text lines). True vertically "
            "downscales and raises a descending glyph only when a glyph on the "
            "following line comes within the required clearance (the "
            "engraved-stroke floor scaled by descending_character_clearance) "
            "of it; false leaves descenders exactly as rendered. Unset falls "
            "back to true."
        ),
    )
    descending_character_v_scale: Optional[float] = Field(
        default=None,
        ge=0.2,
        le=1.0,
        description=(
            "Default deepest vertical downscale permitted for a triggering "
            "descending glyph, in [0.2, 1.0] (job layer; 1.0 = downscaling "
            "forbidden, 0.5 = squeezed to half height, about the glyph's own "
            "baseline). Unset falls back to 0.5."
        ),
    )
    descending_character_raise: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Default deepest raise permitted for a triggering descending "
            "glyph, as a fraction of its own descender depth in [0.0, 1.0] "
            "(job layer; 0.0 = no raising, 1.0 = lift until the glyph's "
            "lowest point sits exactly at the baseline). Unset falls back "
            "to 0.7."
        ),
    )
    descending_character_clearance: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default extra engraved-stroke air gap demanded between a "
            "descending glyph and the glyph below it, as a unitless factor of "
            "the stroke floor at which the two strokes touch (required gap = "
            "floor * (1 + clearance); job layer). An explicit 0.0 is honored "
            "(only true touching triggers). Unset falls back to 0.0."
        ),
    )
    descending_character_reciprocal_scale: Optional[float] = Field(
        default=None,
        ge=0.2,
        le=1.0,
        description=(
            "Default deepest top-down downscale permitted for the "
            "would-be-colliding glyphs on the following line, in [0.2, 1.0] "
            "(job layer; 1.0 = reciprocal downscaling forbidden, 0.5 = "
            "squeezed to half height). The lower glyphs are scaled about their "
            "own lowest ink point, so their lowest point never moves; they "
            "share the relief with the descending glyph through the same ramp. "
            "Unset falls back to 0.5."
        ),
    )
    adjust_h_center_for_single_side_hole: Optional[bool] = Field(
        default=None,
        description=(
            "Default permission to apply horizontal shift for asymmetric "
            "left/right drill holes (job layer). When enabled (true), text is "
            "horizontally shifted to center between left and right holes: shift = "
            "(left_diameter - right_diameter) / 2. Only applied to center-aligned "
            "text; left/right alignment ignores the shift. Vertical shifts (for "
            "top/bottom holes) are always applied and are not gated by this field. "
            "Unset falls back to false."
        ),
    )
    optimize_line_content_max_lines: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Default ceiling N on the consecutive lines a reflow group may "
            "use (job layer). Growth-only: the group starts at its existing "
            "line count and may gain lines up to N. Unset (null) means no "
            "growth."
        ),
    )
    enforce_cap_line: Optional[bool] = Field(
        default=None,
        description=(
            "Default alphanumeric cap-line clamping (job layer). When true, "
            "every ASCII alphanumeric [0-9a-zA-Z] glyph whose ink extends "
            "above the per-font cap height is downscaled top-down so its top "
            "lands exactly on the capline (strictly pre-markup). Unset falls "
            "back to true."
        ),
    )
    bullet_string: Optional[str] = Field(
        default=None,
        description=(
            "Default bullet-list marker string (job layer; cascades item -> "
            "itemize block -> label -> job). Unset falls back to the bullet "
            "glyph '\u2022'."
        ),
    )
    counter_start_index: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Default starting counter value for enumerate lists (job layer; "
            "cascades item -> enumerate block -> label -> job). Unset falls "
            "back to 1."
        ),
    )
    counter_style: Optional[CounterStyle] = Field(
        default=None,
        description=(
            "Default enumerate counter style (job layer; one of decimal, "
            "lower-alpha, upper-alpha, lower-roman, upper-roman). Unset "
            "falls back to decimal."
        ),
    )
    counter_prefix: Optional[str] = Field(
        default=None,
        description=(
            "Default string prepended to each enumerate counter (job layer; "
            "e.g. 'Step ' for 'Step 1'). Unset falls back to '' (no prefix)."
        ),
    )
    counter_suffix: Optional[str] = Field(
        default=None,
        description=(
            "Default string appended to each enumerate counter (job layer; "
            "e.g. '.' for '1.'). Unset falls back to '' (no suffix)."
        ),
    )
    list_marker_gap_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default gap between a list marker and its text, as a fraction "
            "of the line's rendered cap height (job layer). Unset falls "
            "back to 0.5."
        ),
    )
    hole_adjust_for_cutter: Optional[bool] = Field(
        default=None,
        description=(
            "Default cutter-compensation permission for drill holes (job "
            "layer; cascades label -> job, never text lines). When true, the "
            "emitted hole circle shrinks by the boundary/hole cutter diameter "
            "(the cutter radius on each side of the hole's path) so the "
            "engraved hole measures the specified diameter: a 0.1in hole cut "
            "by a 0.015in cutter emits a 0.085in circle. Unset falls back to "
            "true."
        ),
    )
    text_chunk_mode: Optional[Literal["line", "word"]] = Field(
        default=None, description="Default plate-space text optimization granularity."
    )
    layout: Optional[LayoutMode] = Field(
        default=None, description="Default plate fill order (job layer)."
    )
    replacement_text_delimiter: Optional[str] = Field(
        default=None,
        description=(
            "Default replacement-file item delimiter (job layer; cascades "
            "job -> plate -> label): the single special or whitespace "
            "character separating text items within a replacement text file "
            "line. Unset falls back to the code default ';'. Injected only "
            "when the job spec declares a replacement_text_file at the job, "
            "plate, or label level."
        ),
    )
    replacement_text_file_comment_string: Optional[str] = Field(
        default=None,
        description=(
            "Default replacement-file comment marker (job layer; cascades "
            "job -> plate -> label): a replacement text file line whose "
            "whitespace-stripped text starts with this string produces no "
            "label copy and is reported in a per-file WARNING. Unset falls "
            "back to the code default '# '; '' disables comment handling "
            "(every non-blank line is raw data; blank lines are still "
            "dropped). Injected only when the job spec declares a "
            "replacement_text_file at the job, plate, or label level."
        ),
    )
    type: Optional[LabelShape] = Field(
        default=None,
        description=(
            "Default label boundary shape: 'rect' (historical) or 'round' "
            "(ellipse/circle). Job layer; labels that omit it inherit this."
        ),
    )
    rounded_rectangle_fraction: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "Default rectangular-border corner rounding fraction in "
            "[0.0, 1.0] (job layer; 0.0 = sharp rectangle). Superellipse-"
            "quadrant corners with continuously varying curvature; the "
            "rounding spans 'fraction' of the shorter edge. Unset falls back "
            "to 0.0."
        ),
    )
    rounded_rectangle_proportional: Optional[bool] = Field(
        default=None,
        description=(
            "Default proportional corner rounding (job layer). True applies "
            "rounded_rectangle_fraction proportionately to width and height; "
            "false (the default) uses one identical rounding distance on "
            "every side (the shorter edge determines it). Unset falls back "
            "to false."
        ),
    )
    plate_width: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default usable plate width in inches (plate layer + unbounded "
            "auto-allocation; never fills job-level label width)."
        ),
    )
    plate_height: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Default usable plate height in inches (same layer rules as plate_width).",
    )
    left_clearance: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Default plate left-edge clearance in inches (job layer; cascades onto plates that omit it)."
        ),
    )
    top_clearance: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Default plate top-edge clearance in inches (same cascade as left_clearance).",
    )
    auto_line_spacing_interline_to_top_bottom_ratio: Optional[float] = Field(
        default=None,
        gt=0.0,
        description=(
            "Ratio controlling inter-line spacing relative to top/bottom margins "
            "when auto line spacing is enabled. Default 1.0 makes all gaps equal. "
            "Values > 1.0 increase inter-line spacing at the expense of top/bottom margins."
        ),
    )
    tool_options: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional engraver tool options (header commands) metadata. "
            "Keys are underscored header names (e.g., 'cutting_velocity', 'dwell_time'). "
            "Values are dicts with 'command' (HPGL prefix), 'type' (bool/int/float), "
            "'units' (measurement unit string, e.g. 'in/sec'), 'default' (dual "
            "text/borders_holes dict), and for numeric types 'min'/'max' bounds. "
            "Validated at config load: every problem in the file is reported in "
            "one JobConfigError. "
            "Populated by the extract_headers.py script from PLT reference files."
        ),
    )
    preview_background_color: Optional[str] = Field(
        default=None,
        description=(
            "Page/plot background color of the combined 'all_*.pdf' proof "
            "previews, as a #RGB or #RRGGBB hex string (drill holes punch "
            "this same color). Unset falls back to '#D3D3D3' (light grey)."
        ),
    )

    @field_validator("font")
    @classmethod
    def _canonicalize_font_name(cls, value: Optional[str]) -> Optional[str]:
        """Canonicalize the default font against the font registry.

        Applies the same rule the job-spec ``font`` fields use, so a typo in
        the shop config fails at config load (attributed to the config file)
        instead of surfacing later as a ``JobSpec`` validation error that
        points at the job spec.

        Args:
            value: The configured font name, or ``None`` (unset).

        Returns:
            The canonical font name, or ``None`` when unset.

        Raises:
            ValueError: If the name matches no known PLT or TrueType font
                (the message lists every valid name).
        """
        if value is None:
            return None
        return normalize_font_name(value)

    @field_validator("tool_options", mode="before")
    @classmethod
    def _validate_tool_options(cls, value: Any) -> Any:
        """Validate the engraver tool-options metadata.

        Args:
            value: The configured mapping (pre-coercion), or ``None`` (unset).

        Returns:
            The value unchanged, once validated.

        Raises:
            ValueError: If the metadata is malformed (see
                :func:`validate_tool_options_metadata`).
        """
        validate_tool_options_metadata(value)
        return value

    @field_validator("preview_background_color")
    @classmethod
    def _normalize_preview_background_color(cls, value: Optional[str]) -> Optional[str]:
        """Validate/normalize the configured preview background color.

        Args:
            value: The configured hex color, or ``None`` (unset).

        Returns:
            The trimmed hex string, or ``None`` when unset.

        Raises:
            ValueError: If the value is not a #RGB/#RRGGBB hex color.
        """
        return normalize_preview_background_color(value)

    @field_validator("replacement_text_delimiter")
    @classmethod
    def _validate_replacement_delimiter(cls, value: Optional[str]) -> Optional[str]:
        """Validate the configured replacement-file delimiter.

        Applies the shared rule used by every job-spec tier, so a bad shop
        delimiter fails at config load instead of on the first job.

        Args:
            value: The configured delimiter, or ``None`` (unset).

        Returns:
            The validated delimiter.

        Raises:
            ValueError: If the delimiter is not exactly one character, is a
                newline, or is alphanumeric.
        """
        return _validate_replacement_delimiter_value(value)


@dataclass(frozen=True)
class JobConfig:
    """A loaded job-config file with its provenance.

    Attributes:
        path: Path the config was loaded from.
        defaults: Validated default values.
    """

    path: Path
    defaults: JobDefaults


# Job-layer cascade fields mapped from config field -> job-spec key.
# ``plate_width``/``plate_height`` are plate-layer only (see module
# docstring) and ``hole_diameter`` is applied by hole-entry surgery, so
# neither appears here. ``left_clearance``/``top_clearance`` land at the
# job layer and cascade onto plates via ``JobSpec``.
_JOB_LAYER_FIELDS: tuple[str, ...] = (
    "text_height",
    "font",
    "character_spacing",
    "line_spacing",
    "margin",
    "h_margin",
    "v_margin",
    "hole_margin",
    "min_hole_margin",
    "hole_text_collision_distance",
    "cutter_buffer_text",
    "cutter_buffer_routing",
    "max_h_compress",
    "text_h_alignment",
    "space_width_fraction",
    "min_glyph_width_fraction",
    "kerning_window_fraction",
    "kerning_penetration_scale",
    "kerning_recession_scale",
    "kerning_min_gap_fraction",
    "fallback_advance_fraction",
    "kern_pos_string",
    "kern_neg_string",
    "kern_width_fraction",
    "sc_scale",
    "supersubscript_downscale",
    "html_brackets",
    "italic_angle",
    "underline_offset_fraction",
    "underline_break_clearance_fraction",
    "underline_break_spaces",
    "strikethrough_offset_fraction",
    "strikethrough_break_spaces",
    "v_center_chars",
    "target_boldness_factor",
    "multistroke_overlap_ratio",
    "bold_boldness_multiplier",
    "cutter_downsize",
    "max_cutter_downsizes",
    "cutter_downsize_global",
    "cutter_downsize_global_job_wide",
    "h_compress_global",
    "h_compress_global_same_height_only",
    "arc_preserve_compression",
    "optimize_line_content",
    "optimize_line_content_max_lines",
    "enforce_cap_line",
    "bullet_string",
    "counter_start_index",
    "counter_style",
    "counter_prefix",
    "counter_suffix",
    "list_marker_gap_fraction",
    "use_baseline_spacing",
    "v_center_by_content",
    "auto_descending_character_adjustment",
    "descending_character_v_scale",
    "descending_character_raise",
    "descending_character_clearance",
    "descending_character_reciprocal_scale",
    "adjust_h_center_for_single_side_hole",
    "hole_adjust_for_cutter",
    "allow_rotation",
    "text_chunk_mode",
    "layout",
    "replacement_text_delimiter",
    "replacement_text_file_comment_string",
    "type",
    "rounded_rectangle_fraction",
    "rounded_rectangle_proportional",
    "left_clearance",
    "top_clearance",
    "auto_line_spacing_interline_to_top_bottom_ratio",
)

# Config fields whose job-layer injection is suppressed when the spec
# declares the universal ``margin`` (see :func:`_spec_declares_margin`).
_MARGIN_SUPPRESSED_FIELDS: frozenset[str] = frozenset({"h_margin", "v_margin"})

# Config fields whose job-layer injection is suppressed when the spec
# declares no replacement text file at any tier (see
# :func:`_spec_declares_replacement_file`). The schema rejects a job-level
# delimiter/comment marker with no file to apply it to, so both cascading
# replacement fields are gated together.
_REPLACEMENT_SUPPRESSED_FIELDS: frozenset[str] = frozenset(
    {"replacement_text_delimiter", "replacement_text_file_comment_string"}
)


def load_job_config(job_config_path: Optional[Path]) -> Optional[JobConfig]:
    """Load and validate a ``job-config.json`` file.

    Args:
        job_config_path: Path to the config file, or ``None`` to opt out of
            the config layer entirely (parsing then keeps the historical
            all-optional contract for direct API / test callers).

    Returns:
        The validated :class:`JobConfig`. When the path is given but the
        file does not exist, an :attr:`JobDefaults` with no configured
        values is returned — the config layer stays active, so the
        required-when-unconfigured contract still applies (mirroring the
        rule that a missing file means the spec must declare the fields).
        ``None`` is returned only when ``job_config_path`` is ``None``.

    Raises:
        JobConfigError: If the file exists but is not valid JSON, is not a
            JSON object, or fails :class:`JobDefaults` validation.
    """
    if job_config_path is None:
        return None

    if not job_config_path.is_file():
        logger.info(
            "Job config not found at %s; job spec must declare all required fields.",
            job_config_path,
        )
        return JobConfig(path=job_config_path, defaults=JobDefaults())

    try:
        raw_text = job_config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JobConfigError(f"Could not read job config {job_config_path}: {exc}") from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise JobConfigError(f"Invalid JSON in job config {job_config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise JobConfigError(
            f"Job config {job_config_path} must contain a JSON object, got {type(data).__name__}"
        )

    try:
        defaults = JobDefaults(**data)
    except ValidationError as exc:
        raise JobConfigError(f"Invalid job config {job_config_path}:\n{exc}") from exc

    logger.info("Loaded job defaults from %s", job_config_path)
    return JobConfig(path=job_config_path, defaults=defaults)


def _dump_hole_config(hole: HoleSpec) -> dict[str, Any]:
    """Serialize a config hole entry, preserving an omitted ``diameter``.

    :class:`HoleSpec` pre-fills ``diameter`` with its own default, which
    would shadow the configured ``hole_diameter``. Dropping the key when
    the user omitted it keeps the two defaults layered correctly
    (``hole_diameter`` wins over the schema default).

    Args:
        hole: The validated config hole entry.

    Returns:
        A plain mapping ready for injection into the raw job mapping.
    """
    payload = hole.model_dump(mode="json")
    if "diameter" not in hole.model_fields_set:
        payload.pop("diameter", None)
    return payload


def _hole_entries_with_diameters(holes: list[Any], default_diameter: Optional[float]) -> list[Any]:
    """Apply the configured default drill-hole diameter to hole entries.

    Args:
        holes: Raw ``holes`` list entries (dicts from YAML or config).
        default_diameter: Configured ``hole_diameter`` or ``None``.

    Returns:
        The entries with ``diameter`` filled in wherever it was omitted
        and a default is configured; unchanged otherwise.
    """
    if default_diameter is None:
        return holes
    result: list[Any] = []
    for entry in holes:
        if isinstance(entry, dict) and entry.get("diameter") is None:
            entry = {**entry, "diameter": default_diameter}
        result.append(entry)
    return result


def _spec_declares_margin(job_data: Mapping[str, Any]) -> bool:
    """Return True when the raw job mapping declares a universal margin.

    A ``margin`` value at the job level, or on any entry of the raw
    ``labels`` list, is the spec's intent for every label edge. The
    margin cascade resolves ``h_margin`` / ``v_margin`` through
    ``margin`` as their fallback and checks the axis-specific job-level
    keys *first*, so an injected config ``h_margin`` / ``v_margin``
    would silently outrank the spec's margin on that axis -- the
    injection is suppressed whenever this returns True. An explicit
    YAML ``null`` counts as unset (the existing inherit semantics).

    Args:
        job_data: The raw ``job`` mapping parsed from YAML.

    Returns:
        True when the job level or any label declares ``margin``.
    """
    if job_data.get("margin") is not None:
        return True
    labels = job_data.get("labels")
    if isinstance(labels, list):
        return any(isinstance(label, dict) and label.get("margin") is not None for label in labels)
    return False


def _spec_declares_replacement_file(job_data: Mapping[str, Any]) -> bool:
    """Return True when the raw job mapping uses a replacement text file.

    ``replacement_text_file`` is legal at the job, plate, or label level,
    and the job-level ``replacement_text_delimiter`` /
    ``replacement_text_file_comment_string`` cascade job -> plate -> label,
    so ``JobSpec`` rejects either one when the job contains no file at any
    tier. Injecting a configured comment marker into a spec that never uses
    replacement files would therefore fail validation, so the injection is
    suppressed whenever this returns False. An explicit YAML ``null`` counts
    as unset (the existing inherit semantics).

    Args:
        job_data: The raw ``job`` mapping parsed from YAML.

    Returns:
        True when any tier (job, plate, label) declares a file.
    """
    if job_data.get("replacement_text_file") is not None:
        return True
    for key in ("plates", "labels"):
        entries = job_data.get(key)
        if isinstance(entries, list):
            if any(
                isinstance(entry, dict) and entry.get("replacement_text_file") is not None
                for entry in entries
            ):
                return True
    return False


def apply_job_config_defaults(
    job_data: dict[str, Any],
    config: Optional[JobConfig],
) -> dict[str, Any]:
    """Inject configured defaults into a raw job-spec mapping.

    Defaults are applied at the top-most layer only: cascading attributes
    fill missing *job-level* keys (the label -> job cascade then works
    unchanged), and plate defaults fill missing keys of each ``plates``
    entry. Values declared in the YAML are never overridden. The input
    mapping is not mutated.

    The configured ``h_margin`` / ``v_margin`` are additionally suppressed
    whenever the spec declares the universal ``margin`` (job level, or on
    any label): ``margin`` is the spec's intent for every edge, and the
    cascade checks the axis-specific job-level keys before ``margin``,
    so an injected axis value would silently outrank the spec's margin
    on that axis (see :func:`_spec_declares_margin`).

    The configured ``replacement_text_delimiter`` /
    ``replacement_text_file_comment_string`` are suppressed for a spec with no
    replacement text file at any tier, which the schema rejects as job-level
    replacement settings with nothing to apply them to (see
    :func:`_spec_declares_replacement_file`).

    Args:
        job_data: The raw ``job`` mapping parsed from YAML.
        config: The loaded job config, or ``None`` (returned unchanged).

    Returns:
        A new mapping with the configured defaults applied.
    """
    if config is None:
        return job_data

    defaults = config.defaults
    filled: dict[str, Any] = dict(job_data)

    # An explicit YAML ``null`` counts as unset (the cascade treats None as
    # "inherit"), so config values fill absent keys and null-valued ones.
    suppress_margins = _spec_declares_margin(filled)
    suppressed: set[str] = set(_MARGIN_SUPPRESSED_FIELDS) if suppress_margins else set()
    if not _spec_declares_replacement_file(filled):
        suppressed |= _REPLACEMENT_SUPPRESSED_FIELDS
    for field_name in _JOB_LAYER_FIELDS:
        if field_name in suppressed:
            continue
        value = getattr(defaults, field_name)
        if value is not None and filled.get(field_name) is None:
            filled[field_name] = value

    if defaults.holes is not None and filled.get("holes") is None:
        filled["holes"] = [_dump_hole_config(hole) for hole in defaults.holes]

    if "holes" in filled and isinstance(filled["holes"], list):
        filled["holes"] = _hole_entries_with_diameters(filled["holes"], defaults.hole_diameter)

    plates = filled.get("plates")
    if isinstance(plates, list):
        plate_defaults = {
            "width": defaults.plate_width,
            "height": defaults.plate_height,
        }
        filled_plates: list[Any] = []
        for plate in plates:
            if isinstance(plate, dict):
                fill = {
                    key: value
                    for key, value in plate_defaults.items()
                    if value is not None and plate.get(key) is None
                }
                plate = {**fill, **plate}
            filled_plates.append(plate)
        filled["plates"] = filled_plates

    return filled


def assert_required_fields(
    job_data: Mapping[str, Any],
    config: Optional[JobConfig],
) -> None:
    """Enforce the required-when-unconfigured contract.

    Every field in :data:`REQUIRED_WHEN_UNCONFIGURED` must be present at
    the job layer of the (already default-filled) mapping, or declared on
    every label when a label list is used. ``plate_width`` /
    ``plate_height`` are checked separately: unbounded jobs (no ``plates``)
    need the configured plate size, since constrained plates already
    require their dimensions structurally.

    Args:
        job_data: The raw ``job`` mapping after default injection.
        config: The loaded job config, or ``None``. Enforcement is skipped
            entirely without a config (the historical all-optional
            contract for direct API / test callers).

    Raises:
        JobConfigError: If any required field is missing from both the
            config and the spec.
    """
    if config is None:
        return

    # An explicit YAML ``null`` counts as unset (the cascade treats None as
    # "inherit"), so only non-null values satisfy the contract.
    missing: list[str] = [
        name for name in sorted(REQUIRED_WHEN_UNCONFIGURED) if job_data.get(name) is None
    ]

    # A field declared on every label satisfies the contract too (the
    # cascade never needs the job-level value).
    labels = job_data.get("labels")
    if isinstance(labels, list) and labels:
        for name in list(missing):
            if all(isinstance(label, dict) and label.get(name) is not None for label in labels):
                missing.remove(name)

    # Plate dimensions: constrained jobs declare them per plate (already
    # structurally required); unbounded auto-allocation needs the config
    # values, which no job-spec field can supply.
    plate_size_missing: list[str] = []
    has_plates = isinstance(job_data.get("plates"), list) and bool(job_data.get("plates"))
    if not has_plates:
        if config.defaults.plate_width is None:
            plate_size_missing.append("plate_width")
        if config.defaults.plate_height is None:
            plate_size_missing.append("plate_height")

    if missing:
        raise JobConfigError(
            "Job spec is missing required field(s) "
            f"{', '.join(missing)}: declare them in the job spec or define "
            f"defaults in {config.path}"
        )
    if plate_size_missing:
        raise JobConfigError(
            "Job spec has no plates and the job config defines no default "
            f"plate size (missing {', '.join(plate_size_missing)} in "
            f"{config.path}): declare plates in the job spec or define "
            "plate_width / plate_height in the job config"
        )
