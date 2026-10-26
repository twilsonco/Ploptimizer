#!/usr/bin/env python3
"""Generate the Hermes skill tree for the production label-generation agent.

This script renders the *technical* half of the skills that a production
agent (Nous Hermes, agentskills.io format) loads to author job-spec YAML
and run ``plt-optimizer generate`` autonomously. The tree lives under
``docs/schema/skills/engraving-jobs/`` as a single Hermes category
directory of skill directories:

- ``generate-engraving-job/``  the MAIN skill (hand-written prose; this
  script rewrites ONLY the table of contents between the AUTO-TOC markers).
- ``<theme>/``                 one sub-skill per job-authoring topic.
  Each ``SKILL.md`` and each ``references/snippets.yaml`` is GENERATED here;
  ``references/semantics.md`` is CURATED prose that this script links to
  and validates but NEVER overwrites (it seeds a stub only when missing,
  so the production agent's in-place edits survive regeneration).
- ``recipes/``                 complete runnable example specs, copied
  from ``examples/job_specs/`` and validated through ``parse_yaml``.

Field/enum/default tables reuse the renderers in ``generate_ai_docs.py``
(loaded by path, same pattern as ``tests/test_job_spec_docs.py``), so every
table row is byte-identical to ``JOB_SPEC.md``. The theme registry below
must partition EVERY property of EVERY model in the job spec schema exactly
once; the coverage check hard-fails otherwise, so a new schema field can
never ship undocumented.

Hermes progressive disclosure is explicit: ``skill_view(name)`` loads only
``SKILL.md``; reference files load via ``skill_view(name, path)`` when the
agent is told they exist. Every generated file is therefore named in its
own ``SKILL.md`` ("Read next"), which is also what makes Hermes copy it on
install (only files referenced by name are installed).

Usage::

    uv run python docs/schema/generate_skills.py                  # write all
    uv run python docs/schema/generate_skills.py --check          # CI drift
    uv run python docs/schema/generate_skills.py --seed-semantics # stubs only

Output is deterministic (no timestamps, stable ordering, LF newlines).
Pinned by ``tests/test_job_spec_skills.py``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # allow running as a plain script
    sys.path.insert(0, str(REPO_ROOT))

DOCS_DIR = Path(__file__).resolve().parent
CATEGORY_DIR = DOCS_DIR / "skills" / "engraving-jobs"
MAIN_SKILL_DIR = CATEGORY_DIR / "generate-engraving-job"
JOB_SPEC_SCHEMA_JSON = DOCS_DIR / "job_spec.schema.json"
EXAMPLE_SPECS_DIR = REPO_ROOT / "examples" / "job_specs"
AI_DOCS_PATH = DOCS_DIR / "generate_ai_docs.py"

TOC_BEGIN = "<!-- BEGIN AUTO-TOC -->"
TOC_END = "<!-- END AUTO-TOC -->"

#: Heading pair wrapping the live-from-``job-config.json`` tool-options table
#: in the tool-options skill. The drift pin EXCLUDES this region (config-value
#: immunity -- see tests/AGENTS.md), mirroring ``generate_ai_docs.split_tool_options``.
TOOL_OPTIONS_BEGIN = "<!-- BEGIN TOOL OPTIONS (live from job-config.json) -->"
TOOL_OPTIONS_END = "<!-- END TOOL OPTIONS -->"

#: Model rendering order for the per-theme field tables (root model first).
_MODEL_ORDER: Tuple[str, ...] = (
    "JobSpec",
    "PlateSpec",
    "LabelSpec",
    "ContentEntry",
    "TextLine",
    "HoleSpec",
    "EnumerateBlock",
    "ItemizeBlock",
)


def _load_ai_docs() -> Any:
    """Import ``generate_ai_docs.py`` by path to reuse its renderers.

    Returns:
        The loaded generator module.
    """
    spec = importlib.util.spec_from_file_location("generate_ai_docs", AI_DOCS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_ai = _load_ai_docs()


# ---------------------------------------------------------------------------
# Registry data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fragment:
    """A theme-scoped YAML fragment (partial example, injection-cheap)."""

    slug: str
    comment: str
    yaml_text: str


@dataclass(frozen=True)
class Theme:
    """One generated sub-skill: the registry entry driving the renderer."""

    name: str
    title: str
    core: str
    use_when: Tuple[str, ...]
    keywords: Tuple[str, ...]
    fields: Tuple[str, ...]
    enums: Tuple[str, ...] = ()
    pitfalls: Tuple[str, ...] = ()
    see_also: Tuple[str, ...] = ()
    fragments: Tuple[Fragment, ...] = ()
    render_tool_options_table: bool = False
    render_required_gate: bool = False
    extra_refs: Tuple[str, ...] = ()

    @property
    def description(self) -> str:
        """Hermes frontmatter description (core + triggers + keywords)."""
        return (
            f"{self.core} Use when: {'; '.join(self.use_when)}. "
            f"Keywords: {', '.join(self.keywords)}."
        )


@dataclass(frozen=True)
class Recipe:
    """One complete runnable example spec copied into the recipes skill."""

    source: str
    title: str
    caption: str


# ---------------------------------------------------------------------------
# Theme registry -- must be an EXACT partition of every job-spec schema field
# ---------------------------------------------------------------------------

THEMES: Tuple[Theme, ...] = (
    Theme(
        name="job-forms",
        title="Job Forms",
        core="Choose the job structure: labels list, single label, or files.",
        use_when=(
            "starting any new job spec",
            "deciding how labels are defined",
            "a spec fails the label-source validation",
        ),
        keywords=(
            "job form",
            "labels",
            "content",
            "count",
            "single label",
            "job_name",
        ),
        fields=("job_name", "labels", "content", "count", "id", "text"),
        pitfalls=(
            "A job defines exactly ONE label source (`labels`, root `content`, or a "
            "job-level `replacement_text_file`); mixing sources fails validation at "
            "parse time — the pipeline cannot know which one you meant.",
            "Root `content` + `count` is the single-repeating-label form; adding "
            "`labels` alongside it is a mixing error, not an override.",
            "`count` is rejected on a label (or job) that uses a replacement file — "
            "the file's data lines ARE the copies.",
            "`id` is required on every entry of a `labels` list and is the filename "
            "stem of the per-label output; keep ids short, unique, filesystem-safe.",
        ),
        see_also=("replacement-text", "cascade-and-config", "running-and-troubleshooting"),
        fragments=(
            Fragment(
                slug="labels-list",
                comment="Form 1: explicit labels list, each with its own content lines.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Batch 01"\n'
                    "  text_height: 0.5\n"
                    "  labels:\n"
                    "    - id: label_1\n"
                    "      count: 10\n"
                    "      content:\n"
                    '        - text: "Line 1"\n'
                    '        - text: "Line 2"\n'
                ),
            ),
            Fragment(
                slug="single-label",
                comment="Form 2: root-level single label, repeated with count.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Simple Labels"\n'
                    "  count: 20\n"
                    "  content:\n"
                    '    - text: "Single repeating label"\n'
                ),
            ),
        ),
    ),
    Theme(
        name="replacement-text",
        title="Replacement Text Files",
        core="Batch-generate one label per line of a text file.",
        use_when=(
            "generating many labels from a data list",
            "writing replacement_text_file specs",
            "choosing job-, plate-, or label-level files",
        ),
        keywords=(
            "replacement text",
            "batch",
            "data file",
            "delimiter",
            "comment",
            "template",
            "plate pinning",
        ),
        fields=(
            "replacement_text_file",
            "replacement_text_delimiter",
            "replacement_text_file_comment_string",
            "placeholder",
        ),
        pitfalls=(
            "The file path resolves relative to the YAML's directory, not the CWD — "
            "keep the .txt beside the .yaml.",
            "`content` on a replacement-file label is an attribute TEMPLATE (per-line "
            "heights/fonts), not text to engrave; the file's items replace it.",
            "More data items than template lines: the extra lines inherit label-level "
            "attributes. Fewer: the copy renders only as many lines as it has items.",
            '`replacement_text_file_comment_string: ""` is raw-data mode (every '
            'non-blank line engraves); the default `"# "` drops comment lines, '
            "reported as one WARNING per file.",
            "The delimiter must not appear inside the data itself; every data line is "
            "exactly one label copy.",
        ),
        see_also=("job-forms", "cascade-and-config", "plates-layout-and-packing"),
        fragments=(
            Fragment(
                slug="label-level-file",
                comment="Form 3: per-label file; each data line is one label copy.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Batch 02"\n'
                    "  labels:\n"
                    "    - id: badge\n"
                    "      replacement_text_file: data.txt\n"
                    '      replacement_text_delimiter: ";"\n'
                    "      content:\n"
                    '        - text: "PLACEHOLDER L1"\n'
                    "          text_height: 0.45\n"
                ),
            ),
            Fragment(
                slug="job-level-file",
                comment="Form 4: job-level file; job width/height/text_height required.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Batch 03"\n'
                    "  width: 3.0\n"
                    "  height: 1.0\n"
                    "  text_height: 0.75\n"
                    "  replacement_text_file: data.txt\n"
                ),
            ),
        ),
    ),
    Theme(
        name="cascade-and-config",
        title="Cascade Rules, job-config.json and tools.json",
        core="How null inherits, shop defaults inject, and config gates bite.",
        use_when=(
            "a value is not taking effect",
            "deciding which tier to set a field on",
            "`JobConfigError` about a required field",
            "reading the cascade precedence table",
        ),
        keywords=(
            "cascade",
            "inherit",
            "default",
            "job-config",
            "tools.json",
            "fallback",
            "precedence",
        ),
        fields=(),
        render_required_gate=True,
        extra_refs=("references/fallback-defaults.md",),
        pitfalls=(
            "`null`/omitted always means inherit; an explicit value (including 0.0) "
            "always wins at its level — set null to inherit, never delete-and-hope.",
            "`job-config.json` sits below the YAML but above the code fallbacks: shop "
            "defaults are injected at the job layer, so any label can still override them.",
            "With a job-config in play, the required-when-unconfigured fields must come "
            "from config OR spec; missing everywhere aborts with `JobConfigError`.",
            "A spec-declared `margin` (job level, or on any label) suppresses the "
            "config's `h_margin`/`v_margin` injection — the cascade checks the axis keys "
            "before `margin`.",
            "`tools.json` supplies the cutter inventory the ladder snaps to; without it "
            "the ideal cutters are used and the physical tooling may not match.",
        ),
        see_also=("job-forms", "cutters", "label-geometry", "running-and-troubleshooting"),
        fragments=(
            Fragment(
                slug="cascade-tiers",
                comment="Job sets defaults, label overrides, line overrides again.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Cascade demo"\n'
                    "  text_height: 0.5\n"
                    "  labels:\n"
                    "    - id: big\n"
                    "      text_height: 0.8\n"
                    "      content:\n"
                    '        - text: "inherits 0.8 from label"\n'
                    '        - text: "overrides to 0.4"\n'
                    "          text_height: 0.4\n"
                ),
            ),
        ),
    ),
    Theme(
        name="label-geometry",
        title="Label Size, Margins and Shape",
        core="Label dimensions, auto-sizing, margins, rounded corners.",
        use_when=(
            "sizing a label or its margins",
            "making rounded-rectangle labels",
            "text spills outside the border",
        ),
        keywords=(
            "width",
            "height",
            "margin",
            "rounded",
            "shape",
            "border",
            "auto-size",
            "geometry",
        ),
        fields=(
            "width",
            "height",
            "margin",
            "h_margin",
            "v_margin",
            "type",
            "rounded_rectangle_fraction",
            "rounded_rectangle_proportional",
        ),
        enums=("LabelShape",),
        pitfalls=(
            "Omitting `width`/`height` auto-sizes the label from rendered content — the "
            "safe default for single labels; set them for fixed-size stock.",
            "`margin` is the shorthand; `h_margin`/`v_margin` are axis overrides and win "
            "over `margin` at the same tier.",
            "Ink may spill into the margin (only the border stroke counts for collision) "
            "— that is by design, not overflow.",
            "`type: round` is an ELLIPSE boundary (a circle when width == height) and "
            "switches the label to ellipse packing; `rounded_rectangle_fraction` rounds "
            "the corners of a `rect` boundary and is inert on `round` labels.",
            "`rounded_rectangle_fraction` spans that fraction of the SHORTER edge "
            "(superellipse corners, no fillet curvature jump); "
            "`rounded_rectangle_proportional: true` applies it per-axis instead.",
        ),
        see_also=("cascade-and-config", "holes-and-collision", "line-stacking-and-reflow"),
        fragments=(
            Fragment(
                slug="fixed-size-rounded",
                comment="Fixed 3x1.5in label, 0.15in margins, rounded corners.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Rounded placard"\n'
                    "  width: 3.0\n"
                    "  height: 1.5\n"
                    "  margin: 0.15\n"
                    "  rounded_rectangle_fraction: 0.5\n"
                    "  content:\n"
                    '    - text: "ROUNDED CORNERS"\n'
                ),
            ),
        ),
    ),
    Theme(
        name="fonts-and-capline",
        title="Fonts, Small Caps and Cap-Line Enforcement",
        core="Pick fonts (PLT catalog, TTF, SC), text_height, cap-line clamp.",
        use_when=(
            "choosing a font",
            "letters sit off the cap line",
            "using small caps (SC)",
            "setting text height",
        ),
        keywords=(
            "font",
            "text_height",
            "small caps",
            "sc_scale",
            "cap line",
            "enforce_cap_line",
            "ttf",
        ),
        fields=("text_height", "font", "sc_scale", "enforce_cap_line"),
        pitfalls=(
            "`text_height` is nominal: the toolpath height is `text_height - cutter_size`, "
            "so fit math and the engraved cap height both land at the requested size.",
            "Appending ` SC` to any font name selects its synthetic small-caps variant "
            "(uppercase verbatim, lowercase downscaled by `sc_scale`).",
            "`enforce_cap_line` clamps over-tall alphanumerics top-down (strictly "
            "pre-markup: before small-caps, bold, scale, italic and super/subscript); "
            "punctuation and derived glyphs never move.",
            "Valid font names change as the catalog grows — list them with "
            "`python docs/schema/generate_schema_docs.py --show-fonts`.",
        ),
        see_also=("inline-markup", "bold-multistroke", "cutters", "glyph-spacing-and-kerning"),
        fragments=(
            Fragment(
                slug="font-selection",
                comment="Font at job level, per-line override, small-caps variant.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Font demo"\n'
                    '  font: "ReliefSingleLineCAD-Regular"\n'
                    "  content:\n"
                    '    - text: "Main line"\n'
                    '    - text: "Small caps line"\n'
                    '      font: "ReliefSingleLineCAD-Regular SC"\n'
                ),
            ),
        ),
    ),
    Theme(
        name="glyph-spacing-and-kerning",
        title="Glyph Spacing and Kerning",
        core="Per-character advance, space width, auto-kerning tuning.",
        use_when=(
            "letters ride too close or too far",
            "tuning space width",
            "using manual kerning markers in text",
        ),
        keywords=(
            "character_spacing",
            "kerning",
            "space_width_fraction",
            "glyph",
            "advance",
            "kern_pos_string",
        ),
        fields=(
            "character_spacing",
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
        ),
        pitfalls=(
            "These are shop-level tuning knobs; most jobs never set them — start with "
            "`character_spacing` and leave the `kerning_*` family at defaults.",
            "`kern_pos_string`/`kern_neg_string` are manual-kern markers: each occurrence "
            "is stripped (it never engraves) and the NEXT character's cursor advances by "
            "`kern_width_fraction` of the cap height (the negative marker moves it back).",
            "Markers must be non-empty, newline-free, and not markup-shaped (a letters-only "
            "body like `<s>` would be eaten by the tag scan, so the schema rejects it).",
            "`min_glyph_width_fraction` floors how narrow a glyph may get, protecting "
            "legibility when spacing/compression squeeze a line.",
        ),
        see_also=("fonts-and-capline", "inline-markup", "compression-and-cutter-downsize"),
        fragments=(
            Fragment(
                slug="looser-tracking",
                comment="Add breathing room between characters.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Tracking demo"\n'
                    "  character_spacing: 0.12\n"
                    "  content:\n"
                    '    - text: "SPACED OUT"\n'
                ),
            ),
        ),
    ),
    Theme(
        name="line-stacking-and-reflow",
        title="Line Spacing, Baseline Stacking and Reflow",
        core="Stack lines evenly and reflow long text to fit the label width.",
        use_when=(
            "gaps between lines look uneven",
            "a line is too long for the label",
            "wrapping one long sentence across lines",
        ),
        keywords=(
            "line_spacing",
            "baseline",
            "optimize_line_content",
            "reflow",
            "new_line",
            "wrapping",
        ),
        fields=(
            "line_spacing",
            "use_baseline_spacing",
            "auto_line_spacing_interline_to_top_bottom_ratio",
            "new_line",
            "optimize_line_content",
            "optimize_line_content_max_lines",
        ),
        pitfalls=(
            "`use_baseline_spacing` (default true) sets every gap to cap height + "
            "`line_spacing` so descenders hang INTO the gap; `false` restores ink-box "
            "stacking, where one descender-heavy line inflates its own gap.",
            "`optimize_line_content` reflows words into as many lines as needed — it "
            "GROWS the line count, so cap it with `optimize_line_content_max_lines` when "
            "the vertical budget is fixed.",
            "`new_line` starts a new reflow group; it is inert unless the line also "
            "enables `optimize_line_content`, and never enables reflow by itself.",
            "Spacing cascades label -> job -> config only (never text lines) — spacing is "
            "a property of the stacked block, not of one line.",
        ),
        see_also=("centering-and-descender-relief", "label-geometry", "lists"),
        fragments=(
            Fragment(
                slug="reflow",
                comment="Reflow a long sentence to the label width, max 3 lines.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Reflow demo"\n'
                    "  width: 4.0\n"
                    "  optimize_line_content: true\n"
                    "  optimize_line_content_max_lines: 3\n"
                    "  content:\n"
                    '    - text: "A long sentence that must wrap to fit the label width."\n'
                ),
            ),
        ),
    ),
    Theme(
        name="centering-and-descender-relief",
        title="Centering and Descending-Character Relief",
        core="Horizontal/vertical centering and per-glyph descender clearance.",
        use_when=(
            "text looks off-center",
            "g/j/p/q/y collide with the line below",
            "descenders hit the bottom border",
        ),
        keywords=(
            "text_h_alignment",
            "v_center_by_content",
            "descender",
            "auto_descending_character_adjustment",
            "centering",
        ),
        fields=(
            "text_h_alignment",
            "v_center_by_content",
            "v_center_chars",
            "adjust_h_center_for_single_side_hole",
            "auto_descending_character_adjustment",
            "descending_character_v_scale",
            "descending_character_raise",
            "descending_character_clearance",
            "descending_character_reciprocal_scale",
        ),
        enums=("TextHAlignment",),
        pitfalls=(
            "Default vertical centering centers the baseline-to-capline BAND (the optical "
            "center); descenders hang into the bottom margin, where the relief clears them "
            "against the border. `v_center_by_content: true` restores ink-box centering.",
            "Band centering is active ONLY with `auto_descending_character_adjustment` on; "
            "with the relief off nothing would adjust a descender, so ink centering is kept "
            "and the field is inert.",
            "The relief is per colliding glyph, measured as true 2D swept-ink distance "
            "(not bounding boxes), and reciprocal: the glyph below may be scaled top-down "
            "too, capped by `descending_character_reciprocal_scale`.",
            "A text-text collision that nothing can clear aborts the job "
            "(`LabelRenderError`); a BORDER collision never aborts — it engraves the best "
            "allowance permits and logs a WARNING.",
        ),
        see_also=("line-stacking-and-reflow", "holes-and-collision", "inline-markup"),
        fragments=(
            Fragment(
                slug="relief-tuning",
                comment="More descender room: raise the relief higher, allow deeper scale.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Descender demo"\n'
                    "  descending_character_raise: 0.85\n"
                    "  descending_character_v_scale: 0.45\n"
                    "  content:\n"
                    '    - text: "Lighting qty gph"\n'
                    '    - text: "MEG 200A"\n'
                ),
            ),
        ),
    ),
    Theme(
        name="inline-markup",
        title="Inline Markup Tags",
        core="Thirteen in-text tags: style, scale, font, cutter, color, center.",
        use_when=(
            "styling part of a line",
            "mixing fonts or cutters inside one line",
            "splitting strokes by color",
            "superscripts, units, serial prefixes",
        ),
        keywords=(
            "markup",
            "italic",
            "bold",
            "underline",
            "superscript",
            "scale",
            "small caps",
            "text_color",
        ),
        fields=(
            "html_brackets",
            "italic_angle",
            "underline_offset_fraction",
            "underline_break_clearance_fraction",
            "underline_break_spaces",
            "strikethrough_offset_fraction",
            "strikethrough_break_spaces",
            "supersubscript_downscale",
            "text_color",
        ),
        enums=("TextColor",),
        pitfalls=(
            "Tags are inline spans inside a `text:` value and never cross content lines; "
            "nesting is supported. Their tuning fields (`italic_angle`, `underline_*`, "
            "`strikethrough_*`, `supersubscript_downscale`) cascade like everything else.",
            "`<b>` is real multi-pass bold ONLY on multi-stroke PLT fonts; on "
            "single-stroke PLT and TTF fonts it steps to the next larger inventory cutter.",
            "`<f:...>` uses `+` as the canonical space in the font name (e.g. "
            "`<f:Dino+SC>`); the run engraves with the cutter its own font's ladder gives "
            "that text height.",
            "`<c:D>` forces cutter diameter D for the run (snapped to the inventory); "
            "`<oc>` returns to the line's original cutter.",
            "`<tc:color>` splits the run into a separate per-color toolpath (and a "
            "separate PLT file); the `text_color` field colors a whole line.",
            "`html_brackets` is the delimiter pair itself (an even-length string whose "
            "first half opens and second half closes: `<>`, `<<>>`, `«»`); set it to a "
            "pair your data never contains. A bracketed body that is not a registered "
            "tag engraves verbatim, so `3 < 5` is safe, but a real tag name in chevrons "
            "is consumed as markup.",
        ),
        see_also=("fonts-and-capline", "cutters", "bold-multistroke", "lists"),
        fragments=(
            Fragment(
                slug="style-mix",
                comment="Italic, bold and superscript in one line.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Markup demo"\n'
                    "  content:\n"
                    '    - text: "Nom <i>240</i> V / <b>50</b> A"\n'
                    '    - text: "Area 12<sup>2</sup> ft"\n'
                ),
            ),
            Fragment(
                slug="font-cutter-color",
                comment="Inline font switch, cutter force, color split.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Markup demo 2"\n'
                    "  content:\n"
                    '    - text: "Model <f:Dino>X900</f> rev <c:0.030>fine</c>"\n'
                    '    - text: "DANGER <tc:red>high voltage</tc>"\n'
                ),
            ),
        ),
    ),
    Theme(
        name="lists",
        title="Bullet and Enumerated Lists",
        core="itemize/enumerate content blocks with markers and hanging indent.",
        use_when=(
            "engraving a bullet list",
            "numbering items (1. 2. 3. / a. b. c. / roman)",
            "tuning marker gap or counter style",
        ),
        keywords=(
            "itemize",
            "enumerate",
            "bullet",
            "counter",
            "list",
            "marker",
            "hanging indent",
        ),
        fields=(
            "itemize",
            "enumerate",
            "items",
            "bullet_string",
            "counter_start_index",
            "counter_style",
            "counter_prefix",
            "counter_suffix",
            "list_marker_gap_fraction",
        ),
        enums=("CounterStyle",),
        pitfalls=(
            "A content entry carries exactly ONE of `text`, `itemize` or `enumerate` — a "
            "list block is not a text line.",
            "Markers engrave in ONE flush column shared by the whole list and item text "
            "starts at the hanging-indent column (widest marker + "
            "`list_marker_gap_fraction` of the cap height); the list aligns as one block "
            "by `text_h_alignment`, so markers stay flush under center alignment.",
            "The counter is always computed, so an enumerated item's own `bullet_string` "
            'is inert; `bullet_string: ""` gives an indent-only (markerless) list.',
            "A list block cannot be a replacement-text template (its items are authored, "
            "never substituted), and words never cross an item boundary when items reflow.",
        ),
        see_also=("line-stacking-and-reflow", "inline-markup", "fonts-and-capline"),
        fragments=(
            Fragment(
                slug="itemize-and-enumerate",
                comment="A numbered block and a bullet block in one label.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Lists demo"\n'
                    "  content:\n"
                    '    - text: "MAINTENANCE SCHEDULE"\n'
                    "    - enumerate:\n"
                    "        counter_style: upper-roman\n"
                    '        counter_suffix: "."\n'
                    "        items:\n"
                    '          - text: "Inspect belts"\n'
                    '          - text: "Lubricate bearings"\n'
                    "    - itemize:\n"
                    "        items:\n"
                    '          - text: "Shut power first"\n'
                    '          - text: "Wear eye protection"\n'
                ),
            ),
        ),
    ),
    Theme(
        name="cutters",
        title="Cutter Selection",
        core="Auto cutter ladder, explicit cutter_size, per-cutter PLT files.",
        use_when=(
            "choosing or overriding the engraving cutter",
            "output splits into multiple PLT files",
            "a cutter >= text height aborts the job",
        ),
        keywords=(
            "cutter_size",
            "cutter",
            "tools.json",
            "available_cutters",
            "pen",
            "per-cutter",
            "ladder",
        ),
        fields=("cutter_size",),
        pitfalls=(
            "Default `cutter_size: null` auto-selects from `text_height` via the ladder + "
            "`tools.json` inventory; each distinct cutter becomes its own per-cutter PLT "
            "file (one tool load per file on the engraver).",
            "An explicit `cutter_size` snaps to the inventory (next size down, else up, "
            "WARNING when it moves) and the toolpath height becomes `text_height - "
            "cutter_size`, preserving the engraved cap height.",
            "A cutter at or above the line's `text_height` aborts the job "
            "(`CutterSizeError`) — reduce `cutter_size` or raise `text_height`.",
            "`cutter_size` is NOT a `job-config.json` key; set it in the spec (line, "
            "label, or job level).",
        ),
        see_also=(
            "bold-multistroke",
            "compression-and-cutter-downsize",
            "fonts-and-capline",
            "tool-options",
        ),
        fragments=(
            Fragment(
                slug="explicit-cutter",
                comment="Force a 0.040in cutter on one line only.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Cutter demo"\n'
                    "  content:\n"
                    '    - text: "Fine detail"\n'
                    "      cutter_size: 0.040\n"
                    '    - text: "Auto cutter"\n'
                ),
            ),
        ),
    ),
    Theme(
        name="bold-multistroke",
        title="Multi-Stroke (Bold) Fonts",
        core="Stroke-count PLT fonts, target boldness, and <b> behavior.",
        use_when=(
            "engraving bold text on PLT fonts",
            "tuning target_boldness_factor",
            "passes leave unengraved stripes",
        ),
        keywords=(
            "target_boldness_factor",
            "multistroke_overlap_ratio",
            "bold",
            "stroke_count",
            "bold_boldness_multiplier",
        ),
        fields=("target_boldness_factor", "multistroke_overlap_ratio", "bold_boldness_multiplier"),
        pitfalls=(
            "Boldness is cutter-driven on multi-stroke PLT fonts (catalog `stroke_count` "
            "> 1): the ladder sizes the cutter so the N passes total "
            "`target_boldness_factor` of the text height. Inert on single-stroke PLT and "
            "TTF fonts, and on lines with an explicit `cutter_size`.",
            "`multistroke_overlap_ratio` is the anti-stripe floor: adjacent passes always "
            "overlap by at least this fraction, so the derived cutter is never smaller "
            "than `pass_spacing / (1 - ratio)`.",
            "Inline `<b>` multiplies `target_boldness_factor` by "
            "`bold_boldness_multiplier` for the run (clamped, WARNING-logged); on "
            "single-stroke/TTF fonts it steps to the next larger inventory cutter and the "
            "multiplier is inert.",
        ),
        see_also=("cutters", "inline-markup", "fonts-and-capline"),
        fragments=(
            Fragment(
                slug="bolder-strokes",
                comment="Heavier engraved strokes for a bold PLT font.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Bold demo"\n'
                    '  font: "Claret4"\n'
                    "  target_boldness_factor: 0.18\n"
                    "  content:\n"
                    '    - text: "HEAVY PLATE"\n'
                ),
            ),
        ),
    ),
    Theme(
        name="compression-and-cutter-downsize",
        title="Compression and Cutter Downsizing",
        core="Squeeze long text horizontally; swap to a finer cutter when squeezed.",
        use_when=(
            "text overflows the label width",
            "choosing the max_h_compress budget",
            "one text size engraves at two cutters",
        ),
        keywords=(
            "max_h_compress",
            "compression",
            "cutter_downsize",
            "h_compress_global",
            "squeeze",
            "arc_preserve_compression",
        ),
        fields=(
            "max_h_compress",
            "h_compress_global",
            "h_compress_global_same_height_only",
            "arc_preserve_compression",
            "cutter_downsize",
            "max_cutter_downsizes",
            "cutter_downsize_global",
            "cutter_downsize_global_job_wide",
        ),
        pitfalls=(
            "`max_h_compress: 0.0` disables compression entirely (and makes cutter "
            "downsize impossible); the squeeze floor is `1 - max_h_compress`.",
            "Compression fires on margin overflow and on hole collision; the result is a "
            "horizontal scale (glyphs get narrower, height unchanged).",
            "Cutter downsize is ONE-WAY: when a compressed line's effective scale crosses "
            "the midpoint to the next smaller inventory cutter, the line drops a rung (max "
            "`max_cutter_downsizes` rungs, 0 disables) and the toolpath grows to "
            "`text_height - smaller_cutter`. An explicit `cutter_size` is never reduced.",
            "`h_compress_global` / `cutter_downsize_global` share the squeeze/swap across "
            "same-height lines WITHIN a label (one size = one density = one tool); "
            "sharing is always per-label — setting it at job level just enables it for "
            "every label.",
            "Cutter reduction runs BEFORE the shared-compression pre-pass, so a shared "
            "compression can deepen a line's squeeze without re-triggering a cutter swap.",
        ),
        see_also=("cutters", "holes-and-collision", "line-stacking-and-reflow", "label-geometry"),
        fragments=(
            Fragment(
                slug="compression-budget",
                comment="Allow up to 30% horizontal squeeze, shared per label.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Compression demo"\n'
                    "  max_h_compress: 0.3\n"
                    "  h_compress_global: true\n"
                    "  content:\n"
                    '    - text: "A VERY LONG MODEL NUMBER THAT NEEDS TO FIT"\n'
                ),
            ),
        ),
    ),
    Theme(
        name="holes-and-collision",
        title="Drill Holes and Collision Avoidance",
        core="Mounting-hole placement, sizes, and text/hole clearance rules.",
        use_when=(
            "adding mounting holes",
            "holes overlap text",
            "choosing hole margins and collision distances",
        ),
        keywords=(
            "holes",
            "location",
            "corners",
            "sides",
            "hole_margin",
            "min_hole_margin",
            "collision",
            "drill",
        ),
        fields=(
            "holes",
            "location",
            "diameter",
            "hole_margin",
            "hole_adjust_for_cutter",
            "min_hole_margin",
            "hole_text_collision_distance",
        ),
        enums=("HoleLocation",),
        pitfalls=(
            "`location: corners` / `sides` are group shorthands expanded AT VALIDATION "
            "time into the atomic locations; downstream only ever sees the 8 atomic values.",
            "Label `holes` REPLACES the job's list entirely; `holes: []` suppresses the "
            "job's holes on that label.",
            "Collision handling is 3 phases: always-on detection -> opt-in "
            "`min_hole_margin` sweep -> opt-in `max_h_compress` squeeze. Unresolved "
            "collisions abort the job (`LabelRenderError`).",
            "The stroke floor is `0.5 * (hole_cutter + text_cutter) + "
            "hole_text_collision_distance` — raise the distance for more air, not the "
            "hole diameter.",
            "`hole_adjust_for_cutter: true` shrinks the EMITTED circle by the boundary "
            "cutter diameter so the ENGRAVED hole measures `diameter`; the centre never "
            "moves.",
        ),
        see_also=(
            "centering-and-descender-relief",
            "label-geometry",
            "compression-and-cutter-downsize",
            "cutters",
        ),
        fragments=(
            Fragment(
                slug="corner-holes",
                comment="Four corner holes, 0.125in, 0.2in from the edges.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Holes demo"\n'
                    "  width: 4.0\n"
                    "  height: 2.0\n"
                    "  hole_margin: 0.2\n"
                    "  holes:\n"
                    "    - location: corners\n"
                    "      diameter: 0.125\n"
                    "  content:\n"
                    '    - text: "MOUNT HERE"\n'
                ),
            ),
        ),
    ),
    Theme(
        name="plates-layout-and-packing",
        title="Plates, Layout and Bin-Packing",
        core="Pack labels onto material plates: layout, rotation, clearances.",
        use_when=(
            "packing many labels onto stock",
            "declaring plates and materials",
            "pinning labels to one plate",
            "choosing rows vs columns layout",
        ),
        keywords=(
            "plates",
            "layout",
            "packing",
            "rotation",
            "material",
            "clearance",
            "plate_id",
            "text_chunk_mode",
        ),
        fields=(
            "plates",
            "plate_id",
            "layout",
            "allow_rotation",
            "left_clearance",
            "top_clearance",
            "material",
            "text_chunk_mode",
        ),
        enums=("LayoutMode",),
        pitfalls=(
            "With no `plates` declared, plates are auto-allocated at the configured size "
            "(default 24x16); declared plates are used exactly, so labels that do not fit "
            "fail the job.",
            "`plate_id` pins a label to one plate; a plate fed by a replacement file "
            "accepts ONLY its pinned labels.",
            "`layout: columns` (default) packs column-first, `rows` packs row-first; "
            "`allow_rotation: false` keeps labels upright and uses more plate space.",
            "`text_chunk_mode: word` routes each word as its own rapid-travel node (better "
            "travel, more chunk boundaries); `line` (default) is one node per line.",
            "`left_clearance` / `top_clearance` are per-plate keep-out strips (e.g. a "
            "clamped edge), not margins around each label.",
        ),
        see_also=(
            "replacement-text",
            "label-geometry",
            "cascade-and-config",
            "running-and-troubleshooting",
        ),
        fragments=(
            Fragment(
                slug="two-plates",
                comment="Pack onto two named plates, one with a clearance strip.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Plate demo"\n'
                    "  width: 3.0\n"
                    "  height: 1.0\n"
                    "  plates:\n"
                    "    - id: scrap_a\n"
                    "      width: 24.0\n"
                    "      height: 16.0\n"
                    "      left_clearance: 0.5\n"
                    "    - id: full_b\n"
                    "      width: 48.0\n"
                    "      height: 96.0\n"
                    "  labels:\n"
                    "    - id: pinned\n"
                    "      plate_id: scrap_a\n"
                    "      count: 5\n"
                    "      content:\n"
                    '        - text: "SCRAP RUN"\n'
                ),
            ),
        ),
    ),
    Theme(
        name="tool-options",
        title="Tool Options (HPGL Header Commands)",
        core="Engraver velocity, spindle, dwell and Z via tool_options.",
        use_when=(
            "setting cut speed, spindle rpm, dwell, Z clearance",
            "text and borders need different engraver settings",
            "a tool option value gets clamped",
        ),
        keywords=(
            "tool_options",
            "cutting_velocity",
            "spindle_speed",
            "dwell_time",
            "z_clearance",
            "hpgl",
            "header",
        ),
        fields=("tool_options",),
        render_tool_options_table=True,
        pitfalls=(
            "`tool_options` is the one intentional exception to strict mode: its keys are "
            "engraver option names, not schema fields. A key the shop "
            "`job-config.json` does not define is SILENTLY IGNORED (no header, no error), "
            "so spell options exactly as the config defines them.",
            "Each option carries DUAL defaults (text vs borders_holes) from "
            "`job-config.json`; setting one key in the spec applies to both layers.",
            "Out-of-range values are clamped to the configured range with a WARNING — the "
            "job still engraves, at the clamped setting.",
            "The values become HPGL header commands prepended to every generated PLT file, "
            "so they affect the whole file, not one label.",
        ),
        see_also=("cutters", "cascade-and-config", "running-and-troubleshooting"),
        fragments=(
            Fragment(
                slug="custom-speeds",
                comment="Faster cut and higher spindle, at the job level.",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Custom Speeds"\n'
                    "  tool_options:\n"
                    "    cutting_velocity: 1.5\n"
                    "    spindle_speed: 15000\n"
                    "  content:\n"
                    '    - text: "FAST CUT"\n'
                ),
            ),
        ),
    ),
    Theme(
        name="running-and-troubleshooting",
        title="Running the CLI and Fixing Failures",
        core="Generate command, outputs, validation entry, error taxonomy.",
        use_when=(
            "running plt-optimizer generate",
            "a job exits non-zero",
            "locating the PLT and PDF outputs",
            "validating a spec before running",
        ),
        keywords=(
            "generate",
            "cli",
            "parse_yaml",
            "JobConfigError",
            "LabelRenderError",
            "CutterSizeError",
            "output",
            "pdf",
        ),
        fields=(),
        pitfalls=(
            "Validate before running: `parse_yaml(path)` raises `ValueError` "
            "(`JobConfigError` for config-gate violations) naming the exact failing path "
            "(e.g. `labels.0.content.0.tex_height`) — fix the spec.",
            "Outputs land under the output dir (default: the spec's directory): "
            "`plt/*.plt` (one file per cutter, ready for the engraver) and `pdf/*.pdf` "
            "(proof previews).",
            "`--job-config` / `--tools` default to `job-config.json` / `tools.json` in the "
            "CWD; production runs must use the shop files (no config = silent fallbacks; "
            "no tools.json = ideal cutters that may not match the physical tooling).",
            "The pipeline is deterministic: identical input + config + tools engraves "
            "identical output, so only re-run after editing the spec.",
            "`--fast-mode` trades rapid-travel quality for speed; production jobs should "
            "use the default ensemble.",
        ),
        see_also=("cascade-and-config", "job-forms", "plates-layout-and-packing"),
        fragments=(
            Fragment(
                slug="minimal-job",
                comment="The smallest job that generates (validated by parse_yaml).",
                yaml_text=(
                    "job:\n"
                    '  job_name: "Any valid job"\n'
                    "  text_height: 0.5\n"
                    "  content:\n"
                    '    - text: "OK"\n'
                ),
            ),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Recipes registry (complete runnable specs; validated via parse_yaml)
# ---------------------------------------------------------------------------

RECIPES: Tuple[Recipe, ...] = (
    Recipe(
        source="S.yaml",
        title="Multi-line label, auto-sized",
        caption="A plain multi-line label — the baseline shape every spec grows from.",
    ),
    Recipe(
        source="S-compressed.yaml",
        title="Compressed long text",
        caption="A `max_h_compress` budget squeezing a long line into the label width.",
    ),
    Recipe(
        source="lists_demo.yaml",
        title="Bullet and enumerated lists",
        caption="`itemize` / `enumerate` blocks with counters and hanging indent.",
    ),
    Recipe(
        source="replacement_job.yaml",
        title="Batch from a replacement file",
        caption=(
            "A job-level `replacement_text_file` batch. Its data file lives beside the "
            "spec in `examples/job_specs/` — copy both together; paths resolve relative "
            "to the YAML."
        ),
    ),
    Recipe(
        source="round_labels_demo.yaml",
        title="Round labels",
        caption="Circular label shapes with content fitted inside.",
    ),
    Recipe(
        source="rounded_rect_demo.yaml",
        title="Rounded-rectangle labels",
        caption="`rounded_rectangle_fraction` corner rounding on rect labels.",
    ),
    Recipe(
        source="descender_adjust_demo.yaml",
        title="Descender relief showcase",
        caption="Lines with g/j/p/q/y beside caps — the per-glyph relief in action.",
    ),
    Recipe(
        source="2026-10-06 seer.yaml",
        title="Production multi-label job",
        caption=(
            "A real shop job: multiple labels, per-label content and sizing. Pair it with "
            "its sibling data files in `examples/job_specs/` when copying."
        ),
    ),
)


def recipe_slug(source: str) -> str:
    """Slugify a recipe source filename for the examples/ dir.

    Args:
        source: Filename under ``examples/job_specs/``.

    Returns:
        Lowercased name with each run of whitespace replaced by one hyphen.
    """
    return re.sub(r"\s+", "-", source.strip())


# ---------------------------------------------------------------------------
# Schema access
# ---------------------------------------------------------------------------


def load_schema() -> Dict[str, Any]:
    """Load the committed ``job_spec.schema.json``.

    Returns:
        The parsed JSON Schema document.
    """
    schema: Dict[str, Any] = json.loads(JOB_SPEC_SCHEMA_JSON.read_text(encoding="utf-8"))
    return schema


def model_nodes(schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return a model-name -> schema-node mapping in render order.

    Args:
        schema: The job spec JSON Schema document.

    Returns:
        Ordered mapping of model name to its schema node.
    """
    defs: Dict[str, Any] = schema.get("$defs", {})
    nodes: Dict[str, Dict[str, Any]] = {"JobSpec": schema}
    for name in _MODEL_ORDER[1:]:
        if name in defs:
            nodes[name] = defs[name]
    return nodes


def all_field_names(schema: Dict[str, Any]) -> Set[str]:
    """Collect every property name across every model.

    Args:
        schema: The job spec JSON Schema document.

    Returns:
        Set of unique field names.
    """
    names: Set[str] = set()
    for node in model_nodes(schema).values():
        names.update(node.get("properties", {}).keys())
    return names


def coverage_problems(schema: Dict[str, Any]) -> List[str]:
    """Verify the theme registry partitions every schema field exactly once.

    Args:
        schema: The job spec JSON Schema document.

    Returns:
        Human-readable problems (empty when the partition is exact).
    """
    counts: Dict[str, int] = {}
    for theme in THEMES:
        for name in theme.fields:
            counts[name] = counts.get(name, 0) + 1
    problems: List[str] = []
    problems += [
        f"field `{name}` claimed by {count} themes (must be exactly 1)"
        for name, count in sorted(counts.items())
        if count > 1
    ]
    fields = all_field_names(schema)
    missing = sorted(fields - set(counts))
    unknown = sorted(set(counts) - fields)
    if missing:
        problems.append(f"fields not covered by any theme: {missing}")
    if unknown:
        problems.append(f"registry fields not present in the schema: {unknown}")
    return problems


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _frontmatter(name: str, description: str) -> str:
    """Render Hermes frontmatter for a skill.

    Args:
        name: Skill directory name.
        description: Single-line description text (must not contain a quote).

    Returns:
        The `---`-fenced frontmatter block.
    """
    return (
        "---\n"
        f"name: {name}\n"
        f"description: '{description}'\n"
        "version: 1.0.0\n"
        "metadata:\n"
        "  hermes:\n"
        "    category: engraving-jobs\n"
        "    tags: [plt, engraving, toolpath, generate]\n"
        "---\n"
    )


def _field_rows(theme: Theme, schema: Dict[str, Any]) -> str:
    """Render the theme's field table, deduplicated across models.

    A cascading field is declared on many models with identical cells; one
    row per distinct (field, cells) signature keeps the injection unit small,
    with the declaring models named in the `Tiers` column. Cells reuse
    ``generate_ai_docs.py``'s renderers, so each row matches its
    ``JOB_SPEC.md`` counterpart.

    Args:
        theme: The theme being rendered.
        schema: The job spec JSON Schema document.

    Returns:
        Markdown for the ``## Fields`` body (empty when the theme owns none).
    """
    wanted = set(theme.fields)
    order: List[Tuple[str, Tuple[str, str, str, str]]] = []
    tiers: Dict[Tuple[str, Tuple[str, str, str, str]], List[str]] = {}
    for model_name, node in model_nodes(schema).items():
        props: Dict[str, Any] = node.get("properties", {})
        required = set(node.get("required", []))
        for field_name in props:
            if field_name not in wanted:
                continue
            fnode: Dict[str, Any] = props[field_name]
            cells = (
                _ai._type_label(fnode),
                _ai._default_label(fnode, field_name in required),
                _ai._constraints_label(fnode),
                _ai._md_cell(fnode.get("description", "")),
            )
            key = (field_name, cells)
            if key not in tiers:
                order.append(key)
                tiers[key] = []
            tier = _TIER_NAMES.get(model_name, model_name)
            if tier not in tiers[key]:
                tiers[key].append(tier)

    lines = [_FIELD_TABLE_HEADER]
    for key in order:
        field_name, (type_label, default_label, constraints, description) = key
        lines.append(
            f"| `{field_name}` | {type_label} | {default_label} | {constraints} | "
            f"{' / '.join(tiers[key])} | {description} |"
        )
    return "\n".join(lines)


_FIELD_TABLE_HEADER = (
    "| Field | Type | Default | Constraints | Tiers | Description |\n|---|---|---|---|---|---|"
)

#: Friendly tier names for the `Tiers` column (author-facing vocabulary).
_TIER_NAMES: Dict[str, str] = {
    "JobSpec": "job",
    "PlateSpec": "plate",
    "LabelSpec": "label",
    "ContentEntry": "content entry",
    "TextLine": "line",
    "HoleSpec": "hole",
    "EnumerateBlock": "list block",
    "ItemizeBlock": "list block",
}


def _enum_sections(theme: Theme, schema: Dict[str, Any]) -> str:
    """Render the theme's enum tables.

    Args:
        theme: The theme being rendered.
        schema: The job spec JSON Schema document.

    Returns:
        Markdown for the ``## Enums`` body (empty when the theme owns none).
    """
    defs: Dict[str, Any] = schema.get("$defs", {})
    return "\n\n".join(_ai._render_enum(name, defs[name]) for name in theme.enums if name in defs)


def _field_to_constant(field_name: str) -> str:
    """Map a schema field name to its fallback constant name.

    Args:
        field_name: Schema property name (snake_case).

    Returns:
        The ``DEFAULT_<UPPERCASED>`` constant name.
    """
    return "DEFAULT_" + field_name.upper()


#: Fallback constants that do not match a field name, routed by hand. Every
#: other unmatched constant lands in `cascade-and-config` (the catch-all), so
#: the generated tables together stay complete.
_CONSTANT_OWNER: Dict[str, str] = {
    "DEFAULT_BOUNDARY_HOLE_CUTTER": "holes-and-collision",
    "DEFAULT_CUTTER_LADDER_TOLERANCE": "cutters",
    "DEFAULT_FONT_NAME": "fonts-and-capline",
    "DEFAULT_LABEL_SHAPE": "label-geometry",
    "DEFAULT_HOLE_DIAMETER": "holes-and-collision",
    "DEFAULT_PLATE_WIDTH": "plates-layout-and-packing",
    "DEFAULT_PLATE_HEIGHT": "plates-layout-and-packing",
}


def _owns_constant(theme: Theme, name: str, field_constants: Set[str]) -> bool:
    """Decide whether a theme renders one fallback constant row inline.

    Args:
        theme: The theme being rendered.
        name: Constant name.
        field_constants: Constant names derived from the theme's fields.

    Returns:
        True when this theme owns the constant.
    """
    owner = _CONSTANT_OWNER.get(name)
    if owner is not None:
        return owner == theme.name
    if name in field_constants:
        return True
    # Unmatched constants live only in the generated catch-all reference
    # (references/fallback-defaults.md), keeping skill bodies lean.
    return False


def _defaults_rows(theme: Theme, constants: Dict[str, str]) -> str:
    """Render the fallback-constant table for a theme.

    Args:
        theme: The theme being rendered.
        constants: The full scraped constant mapping.

    Returns:
        Markdown for the ``## Defaults`` body (empty when the theme owns none).
    """
    field_constants = {_field_to_constant(name) for name in theme.fields}
    rows = [
        f"| `{name}` | `{value}` | {_ai._md_cell(_ai._FALLBACK_NOTES.get(name, '—'))} |"
        for name, value in sorted(constants.items())
        if _owns_constant(theme, name, field_constants)
    ]
    if not rows:
        return ""
    return "\n".join(
        [
            "Applied when unset at every tier (including `job-config.json`):",
            "",
            "| Constant | Value | Note |",
            "|---|---|---|",
        ]
        + rows
    )


#: One-line notes for the optional extra reference files a theme declares.
_REF_NOTES: Dict[str, str] = {
    "references/fallback-defaults.md": (
        "The complete resolved-fallback-constants table (every `DEFAULT_*`, "
        "scraped live from source)"
    ),
}


def ref_note(ref: str) -> str:
    """Return the one-line note for an extra reference file.

    Args:
        ref: Reference path relative to the skill directory.

    Returns:
        The curated note, or a generic fallback.
    """
    return _REF_NOTES.get(ref, "additional generated reference")


def render_fallback_defaults_md(constants: Dict[str, str]) -> str:
    """Render the complete fallback-constant table as a reference file.

    The per-skill ``## Defaults`` sections carry only the constants their
    fields own; this catch-all keeps every constant documented and skill
    bodies lean (Hermes loads it via Level-2 disclosure on demand).

    Args:
        constants: The full scraped constant mapping.

    Returns:
        Markdown text for ``references/fallback-defaults.md``.
    """
    lines = [
        "# Resolved fallback defaults — all constants",
        "",
        "<!-- AUTO-GENERATED by docs/schema/generate_skills.py - DO NOT EDIT. -->",
        "<!-- Regenerate: uv run python docs/schema/generate_skills.py -->",
        "",
        "Applied when a field is unset at every tier (spec -> job-config.json).",
        "Values are scraped from `resolution.py` / `schema.py` / `layout.py`",
        "source, so this table is always current.",
        "",
        "| Constant | Value | Note |",
        "|---|---|---|",
    ]
    for name, value in sorted(constants.items()):
        note = _ai._md_cell(_ai._FALLBACK_NOTES.get(name, "—"))
        lines.append(f"| `{name}` | `{value}` | {note} |")
    return "\n".join(lines) + "\n"


def _tool_options_block() -> str:
    """Render the live tool-options table, wrapped in the pin-exclusion markers.

    Returns:
        The marked-up markdown block (empty when the config has no tool_options).
    """
    options = _ai._load_tool_options()
    if not options:
        return ""
    lines = _ai._format_tool_options_markdown(options)
    body = [ln for ln in lines if not ln.startswith(_ai.TOOL_OPTIONS_HEADING)]
    return "\n".join([TOOL_OPTIONS_BEGIN, ""] + body + [TOOL_OPTIONS_END]).strip()


def _required_gate_block() -> str:
    """Render the required-when-unconfigured field list from the live model.

    Returns:
        A markdown bullet list.
    """
    names = [f"- `{name}`" for name in sorted(_ai.REQUIRED_WHEN_UNCONFIGURED)]
    names.append("- `plate_width` / `plate_height` (only when the job declares no plates)")
    return "\n".join(names)


def _example_block(theme: Theme) -> str:
    """Render the theme's inline example (its first fragment).

    Args:
        theme: The theme being rendered.

    Returns:
        Markdown for the ``## Example`` body.
    """
    if not theme.fragments:
        return "_See `references/snippets.yaml` for examples._"
    fragment = theme.fragments[0]
    return f"{fragment.comment}\n\n```yaml\n{fragment.yaml_text}```"


def render_skill_md(theme: Theme, schema: Dict[str, Any], constants: Dict[str, str]) -> str:
    """Render one generated sub-skill ``SKILL.md``.

    Args:
        theme: The theme registry entry.
        schema: The job spec JSON Schema document.
        constants: The scraped fallback constants.

    Returns:
        The full SKILL.md text (deterministic, trailing newline included).
    """
    parts: List[str] = [_frontmatter(theme.name, theme.description)]
    parts.append(f"# {theme.title}\n")
    parts.append("## When to use\n\n" + "\n".join(f"- {w}" for w in theme.use_when) + "\n")

    fields = _field_rows(theme, schema)
    if fields:
        parts.append("## Fields\n\n" + fields + "\n")

    enums = _enum_sections(theme, schema)
    if enums:
        parts.append("## Enums\n\n" + enums + "\n")

    defaults = _defaults_rows(theme, constants)
    if defaults:
        parts.append("## Defaults\n\n" + defaults + "\n")

    if theme.render_tool_options_table:
        block = _tool_options_block()
        if block:
            parts.append("## Available options\n\n" + block + "\n")

    if theme.render_required_gate:
        parts.append(
            "## Required-when-unconfigured (job-config gate)\n\n"
            "With a `--job-config` in play, these must come from the config **or** the "
            "spec, else parsing aborts with `JobConfigError`:\n\n" + _required_gate_block() + "\n"
        )

    parts.append("## Example\n\n" + _example_block(theme) + "\n")
    parts.append("## Pitfalls\n\n" + "\n".join(f"- {p}" for p in theme.pitfalls) + "\n")

    read_next = [
        "- `references/semantics.md` — the full semantics for this topic: mechanisms, "
        "thresholds, tier interactions and worked examples the tables above cannot "
        "express. Read it before authoring anything non-trivial in this area.",
        "- `references/snippets.yaml` — more YAML fragments for this topic.",
    ]
    read_next += [f"- `{ref}` — {ref_note(ref)}." for ref in theme.extra_refs]
    read_next += [
        "- [`../../../JOB_SPEC.md`](../../../JOB_SPEC.md) — exhaustive field/enum/default "
        "reference for the whole spec.",
        "- [`../../../job_spec.schema.json`](../../../job_spec.schema.json) — machine-readable "
        "schema for structural validation.",
    ]
    if theme.see_also:
        siblings = "; ".join(f"[{s}](../{s}/SKILL.md)" for s in theme.see_also)
        read_next.append(f"- Related skills: {siblings}.")
    parts.append("## Read next\n\n" + "\n".join(read_next) + "\n")
    return "\n".join(parts)


def render_snippets_yaml(theme: Theme) -> str:
    """Render a theme's ``references/snippets.yaml``.

    Args:
        theme: The theme registry entry.

    Returns:
        Valid YAML text (deterministic, trailing newline included).
    """
    lines = [
        f"# YAML fragments for the {theme.name} skill.",
        "# AUTO-GENERATED by docs/schema/generate_skills.py - DO NOT EDIT.",
        "# Regenerate: uv run python docs/schema/generate_skills.py",
        "# Partial examples: valid YAML under `job:`, not complete runnable specs.",
        "# For complete runnable examples see ../recipes/SKILL.md.",
    ]
    for fragment in theme.fragments:
        lines.append(f"{fragment.slug}:")
        lines.append(f"  # {fragment.comment}")
        lines.append("  yaml: |")
        for ln in fragment.yaml_text.rstrip("\n").splitlines():
            lines.append(f"    {ln}")
    return "\n".join(lines) + "\n"


_SEMANTICS_STUB = """# {title} — semantics

<!--
CURATED FILE - hand-written and edited in place. The skill generator links to
and validates this file but NEVER overwrites it (it seeds this stub only when
the file is missing), so edits survive regeneration.
-->

Document what the schema tables in `../SKILL.md` cannot express for {title}:
the mechanisms, thresholds, tier interactions, and worked YAML examples.
"""


def semantics_stub(theme_title: str) -> str:
    """Render the placeholder semantics file for a theme.

    Args:
        theme_title: The theme's human title.

    Returns:
        The stub markdown text.
    """
    return _SEMANTICS_STUB.format(title=theme_title)


def render_recipes_skill_md() -> str:
    """Render ``recipes/SKILL.md`` (complete runnable examples).

    Returns:
        The full SKILL.md text.
    """
    parts = [
        _frontmatter(
            "recipes",
            "Complete runnable job specs to copy and adapt. Use when: "
            "starting a job from a known-good shape; reading a full spec "
            "end to end. Keywords: example, recipe, template, yaml, copy.",
        )
    ]
    parts.append("# Recipes — Complete Example Job Specs\n")
    parts.append(
        "## When to use\n\n"
        "- Starting a new job: pick the closest shape below, copy its YAML, adapt it.\n"
        "- Learning how the fields combine in a real, validated spec.\n"
    )
    parts.append(
        "Every example below is copied verbatim from `examples/job_specs/` in the tool "
        "repository and re-validated through `parse_yaml` on every regeneration. The "
        "files live in `examples/` beside this skill. Replacement data files (where a "
        "recipe uses one) live beside the ORIGINAL spec and must be copied with it — "
        "paths resolve relative to the YAML file's directory.\n"
    )
    for recipe in RECIPES:
        slug = recipe_slug(recipe.source)
        source_text = (EXAMPLE_SPECS_DIR / recipe.source).read_text(encoding="utf-8")
        parts.append(f"## {recipe.title}\n\n{recipe.caption}\n")
        parts.append(f"File: [`examples/{slug}`](examples/{slug})\n")
        parts.append(f"```yaml\n{source_text.rstrip()}\n```\n")
    parts.append(
        "## Read next\n\n"
        "- `references/semantics.md` — choosing and adapting a recipe.\n"
        "- [`../generate-engraving-job/SKILL.md`](../generate-engraving-job/SKILL.md) — "
        "the main workflow skill.\n"
        "- [`../../../JOB_SPEC.md`](../../../JOB_SPEC.md) — exhaustive field reference.\n"
    )
    return "\n".join(parts)


def render_toc_lines() -> str:
    """Render the main skill's AUTO-TOC body (one bullet per sub-skill).

    Returns:
        Markdown bullets for every sub-skill plus recipes, in registry order.
    """
    lines = ["<!-- generated by docs/schema/generate_skills.py - do not edit this block -->"]
    lines += [f"- [{t.title}](../{t.name}/SKILL.md) — {t.core}" for t in THEMES]
    lines.append(
        "- [Recipes — Complete Example Job Specs](../recipes/SKILL.md) — "
        "copy-and-adapt runnable specs."
    )
    return "\n".join(lines)


def inject_toc(main_skill_text: Optional[str]) -> str:
    """Replace the AUTO-TOC block in the main skill text.

    Args:
        main_skill_text: The current main SKILL.md text, or None when the file
            does not exist yet (a placeholder carrying the markers is created).

    Returns:
        The updated main SKILL.md text.
    """
    toc = render_toc_lines()
    if main_skill_text is None:
        return (
            "# Generate Engraving Job\n\n"
            "(placeholder — the main-skill author owns this prose; the generator "
            f"maintains only the table of contents between the markers)\n\n"
            f"{TOC_BEGIN}\n{toc}\n{TOC_END}\n"
        )
    begin = main_skill_text.find(TOC_BEGIN)
    end = main_skill_text.find(TOC_END)
    if begin < 0 or end < 0 or end < begin:
        raise SystemExit(
            "main SKILL.md: AUTO-TOC markers missing; add the "
            f"`{TOC_BEGIN}` and `{TOC_END}` lines around the table of contents."
        )
    head = main_skill_text[: begin + len(TOC_BEGIN)]
    tail = main_skill_text[end:]
    return f"{head}\n{toc}\n{tail}"


# ---------------------------------------------------------------------------
# Validation shared with the test suite
# ---------------------------------------------------------------------------

_SNAKE_RE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")


def fragment_problems(schema: Dict[str, Any]) -> List[str]:
    """Validate every registry fragment: parseable YAML, real field names.

    Args:
        schema: The job spec JSON Schema document.

    Returns:
        Human-readable problems (empty when every fragment is clean).
    """
    allowed = all_field_names(schema) | set(_ai._load_tool_options())
    problems: List[str] = []

    def walk(node: Any, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and _SNAKE_RE.fullmatch(key) and key not in allowed:
                    problems.append(f"{where}: unknown field `{key}`")
                walk(value, where)
        elif isinstance(node, list):
            for item in node:
                walk(item, where)

    for theme in THEMES:
        for fragment in theme.fragments:
            where = f"{theme.name}/{fragment.slug}"
            try:
                parsed = yaml.safe_load(fragment.yaml_text)
            except yaml.YAMLError as exc:
                problems.append(f"{where}: invalid YAML: {exc}")
                continue
            if not isinstance(parsed, dict) or "job" not in parsed:
                problems.append(f"{where}: fragment must be a `job:` mapping")
                continue
            walk(parsed["job"], where)
    return problems


def recipe_problems() -> List[str]:
    """Validate that every recipe source parses through ``parse_yaml``.

    Returns:
        Human-readable problems (empty when every recipe parses).
    """
    from plt_optimizer.generate.schema import parse_yaml

    problems: List[str] = []
    for recipe in RECIPES:
        path = EXAMPLE_SPECS_DIR / recipe.source
        if not path.exists():
            problems.append(f"{recipe.source}: missing under examples/job_specs/")
            continue
        try:
            parse_yaml(path)
        except (ValueError, FileNotFoundError) as exc:
            problems.append(f"{recipe.source}: parse_yaml failed: {exc}")
    return problems


def registry_problems(schema: Optional[Dict[str, Any]] = None) -> List[str]:
    """Run every registry-level check (coverage, fragments, recipes).

    Args:
        schema: The job spec schema, loaded when omitted.

    Returns:
        Human-readable problems (empty when the registry is sound).
    """
    doc = load_schema() if schema is None else schema
    return coverage_problems(doc) + fragment_problems(doc) + recipe_problems()


# ---------------------------------------------------------------------------
# File plan / writing / checking
# ---------------------------------------------------------------------------


def build_file_plan() -> Dict[Path, str]:
    """Render every generated file to text (no disk writes).

    Returns:
        Mapping of absolute path -> generated content, in stable order.
    """
    schema = load_schema()
    constants = _ai._scrape_constants()
    plan: Dict[Path, str] = {}
    for theme in THEMES:
        skill_dir = CATEGORY_DIR / theme.name
        plan[skill_dir / "SKILL.md"] = render_skill_md(theme, schema, constants)
        plan[skill_dir / "references" / "snippets.yaml"] = render_snippets_yaml(theme)
    recipes_dir = CATEGORY_DIR / "recipes"
    plan[recipes_dir / "SKILL.md"] = render_recipes_skill_md()
    plan[CATEGORY_DIR / "cascade-and-config" / "references" / "fallback-defaults.md"] = (
        render_fallback_defaults_md(constants)
    )
    main_path = MAIN_SKILL_DIR / "SKILL.md"
    plan[main_path] = inject_toc(
        main_path.read_text(encoding="utf-8") if main_path.exists() else None
    )
    return plan


def seed_semantics() -> List[Path]:
    """Create ``references/semantics.md`` stubs for every theme missing one.

    Returns:
        The paths created.
    """
    created: List[Path] = []
    entries: Sequence[Tuple[str, str]] = [(t.name, t.title) for t in THEMES] + [
        ("recipes", "Recipes")
    ]
    for name, title in entries:
        path = CATEGORY_DIR / name / "references" / "semantics.md"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(semantics_stub(title), encoding="utf-8")
            created.append(path)
    return created


def write_all(plan: Dict[Path, str]) -> int:
    """Write every generated file, creating parent directories as needed.

    Args:
        plan: Mapping of path -> content from :func:`build_file_plan`.

    Returns:
        The number of files whose contents changed.
    """
    changed = 0
    for path, content in plan.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            changed += 1
    examples_dir = CATEGORY_DIR / "recipes" / "examples"
    examples_dir.mkdir(parents=True, exist_ok=True)
    for recipe in RECIPES:
        shutil.copyfile(
            EXAMPLE_SPECS_DIR / recipe.source, examples_dir / recipe_slug(recipe.source)
        )
    return changed


def strip_tool_options_region(text: str) -> str:
    """Remove the live-from-config tool-options region for drift comparison.

    Args:
        text: A rendered skill file.

    Returns:
        The text with the marked region collapsed.
    """
    begin = text.find(TOOL_OPTIONS_BEGIN)
    end = text.find(TOOL_OPTIONS_END)
    if begin >= 0 and end > begin:
        return text[:begin] + text[end:]
    return text


def check_all(plan: Dict[Path, str]) -> List[Path]:
    """Compare the generated files on disk against a fresh render.

    The tool-options live table region is excluded from the comparison
    (config-value immunity, mirroring the ``JOB_SPEC.md`` drift pin).

    Args:
        plan: Mapping of path -> content from :func:`build_file_plan`.

    Returns:
        Paths that are stale or missing.
    """
    stale: List[Path] = []
    for path, content in plan.items():
        if not path.exists():
            stale.append(path)
            continue
        if strip_tool_options_region(path.read_text(encoding="utf-8")) != strip_tool_options_region(
            content
        ):
            stale.append(path)
    for recipe in RECIPES:
        src = EXAMPLE_SPECS_DIR / recipe.source
        dst = CATEGORY_DIR / "recipes" / "examples" / recipe_slug(recipe.source)
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            stale.append(dst)
    return stale


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Render in memory and exit 1 if any generated file is stale.",
    )
    parser.add_argument(
        "--seed-semantics",
        action="store_true",
        help="Create stub references/semantics.md only where missing; write nothing else.",
    )
    args = parser.parse_args(argv)

    problems = registry_problems()
    if problems:
        for problem in problems:
            print(f"registry problem: {problem}", file=sys.stderr)
        return 1

    if args.seed_semantics:
        created = seed_semantics()
        for path in created:
            print(f"seeded {path.relative_to(REPO_ROOT)}")
        if not created:
            print("all semantics.md files present; nothing seeded")
        return 0

    plan = build_file_plan()
    if args.check:
        stale = check_all(plan)
        for path in stale:
            print(f"stale: {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        if stale:
            print("run: uv run python docs/schema/generate_skills.py", file=sys.stderr)
            return 1
        print(f"all {len(plan)} generated skill files are up to date")
        return 0

    changed = write_all(plan)
    seeded = seed_semantics()
    print(
        f"wrote {changed} of {len(plan)} generated files under "
        f"{CATEGORY_DIR.relative_to(REPO_ROOT)}"
    )
    if seeded:
        print(f"seeded {len(seeded)} semantics.md stubs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
