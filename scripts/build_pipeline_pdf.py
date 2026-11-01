#!/usr/bin/env python3
"""Build a printable PDF handbook for the generate pipeline.

Assembles one landscape-letter PDF from the repository's *committed*
documentation artifacts (the generated ``JOB_SPEC.md`` / ``JOB_CONFIG.md``
references, the Hermes skill tree, the curated ``semantics.md`` files) plus
live output from ``plt-optimizer generate`` for a curated set of example specs,
rasterized to page images of the rendered proofs.

The script is a **renderer**: it never re-derives schema content and never edits
a source document, so it cannot drift from the Pydantic models. The drift pins
(``tests/test_job_spec_docs.py``, ``tests/test_job_spec_skills.py``,
``tests/test_prose_markdown.py``) stay the only sources of truth.

Toolchain (external build-time tools, preflighted with ``shutil.which``):

- ``pandoc`` + ``xelatex`` - markdown -> PDF
- ``ghostscript`` (``gs``) - rasterize the pipeline's PDF proofs to PNG

Usage::

    uv run python scripts/build_pipeline_pdf.py
    uv run python scripts/build_pipeline_pdf.py --markdown-only
    uv run python scripts/build_pipeline_pdf.py --jobs curated --no-previews
    uv run python scripts/build_pipeline_pdf.py --sections cover,quickstart,spec

Output goes to ``docs/pdf/plt-generation-pipeline.pdf`` (gitignored - rebuild it
with this script; never hand-edit or byte-pin it).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # allow running as a plain script
    sys.path.insert(0, str(REPO_ROOT))

SKILL_ROOT = REPO_ROOT / "docs" / "skills" / "engraving-jobs"
REFERENCE_ROOT = SKILL_ROOT / "engraving-job-complete-reference" / "references"
EXAMPLES_ROOT = REPO_ROOT / "examples" / "job_specs"
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "pdf" / "plt-generation-pipeline.pdf"

#: Lines of a batch data file (``replacement_text_file``) printed verbatim.
DATA_FILE_LINES = 20
#: Rasterization density for the embedded proof previews (DPI).
DEFAULT_DPI = 200
#: Generated references rendered as Parts I/II, so excluded from Part III.
PART_REFERENCES = frozenset({"JOB_SPEC.md", "JOB_CONFIG.md"})

logger = logging.getLogger("build_pipeline_pdf")


# --------------------------------------------------------------------------- #
# Content tables
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ShowcaseJob:
    """One curated example spec to feature in the examples part."""

    spec: str
    blurb: str
    data_files: Tuple[str, ...] = ()


#: The curated showcase: one example per feature family (see docs/pdf/README.md).
SHOWCASE_JOBS: Tuple[ShowcaseJob, ...] = (
    ShowcaseJob(
        "S.yaml",
        "Minimal job: the root-label form (`content` + `count`), every default "
        "inherited from `job-config.json`.",
    ),
    ShowcaseJob(
        "S-compressed.yaml",
        "Horizontal compression: `max_h_compress` squeezes a long line to fit "
        "the label width instead of failing.",
    ),
    ShowcaseJob(
        "lists_demo.yaml",
        "List blocks: numbered/lettered items, marker styles, hanging text "
        "columns, and nested list layout.",
    ),
    ShowcaseJob(
        "replacement_job.yaml",
        "Replacement-text form: one label per line of an external `.txt` data "
        "file, with field tags and comment lines.",
        data_files=("replacement_text_assets.txt",),
    ),
    ShowcaseJob(
        "round_labels_demo.yaml",
        "Circular label shape with drill holes on a bolt-circle pattern.",
    ),
    ShowcaseJob(
        "rounded_rect_demo.yaml",
        "Rounded-rectangle placard: corner radii, border strokes, margins.",
    ),
    ShowcaseJob(
        "descender_adjust_demo.yaml",
        "Descending-character relief: `g j p q y` descenders cleared against "
        "the caps on the line below.",
    ),
    ShowcaseJob(
        "2026-10-07 coolsys 1 big label sc.yaml",
        "Synthetic small caps: the ` SC` font suffix and `<sc>` markup on a "
        "large multi-line label.",
    ),
    ShowcaseJob(
        "2026-10-06 seer intrachunk opt TTF.yaml",
        "TrueType font (`.ttf`) rendering with intra-chunk travel optimization enabled.",
    ),
    ShowcaseJob(
        "2026-10-06 seer.yaml",
        "A real multi-label production job: explicit `labels` list, per-label "
        "overrides, plates, and cutters.",
    ),
    ShowcaseJob(
        "2026-10-15 coolsys round tags target 910.yaml",
        "Batch of round tags driven by a 127-line data file across material plates.",
        data_files=("2026-10-15 coolsys round tags target 910 b.txt",),
    ),
    ShowcaseJob(
        "2026-10-30 CES vons 1735.yaml",
        "Multi-plate bin packing with material/color tags (the `bw_` material "
        "prefix) and per-cutter output files.",
        data_files=(
            "2026-10-30 CES vons 1735 1x3.txt",
            "2026-10-30 CES vons 1735 2x4.txt",
            "2026-10-30 CES vons 1735 7x7.txt",
        ),
    ),
)

#: The job used to demonstrate every artifact ``generate`` writes.
GALLERY_JOB = "2026-10-30 CES vons 1735.yaml"

#: Filename patterns of the generated artifacts and their role (Part V).
ARTIFACT_ROLES: Tuple[Tuple[str, str], ...] = (
    (
        "plt/[<plate>_][<material>_]<cutter>[_<color>]_bh_<job_id>.plt",
        "Borders + drill holes (`bh`) for one (plate, material, cutter, color) "
        "group - typically the first tool loaded on the engraver.",
    ),
    (
        "plt/[<plate>_][<material>_]<cutter>[_<color>]_txt_<job_id>.plt",
        "Text strokes for the same group - load with that cutter.",
    ),
    (
        "pdf/[<plate>_][<material>_]<cutter>[_<color>]_bh_<job_id>.pdf",
        "Simple-outline proof of one borders/holes PLT.",
    ),
    (
        "pdf/[<plate>_][<material>_]<cutter>[_<color>]_txt_<job_id>.pdf",
        "Simple-outline proof of one text PLT (stroke width = the engraved cutter width).",
    ),
    (
        "pdf/[<plate>_][<material>_]all_<job_id>.pdf",
        "Combined colored proof per plate: every pen on one page, stroke "
        "colors from the layer/material palette, drill holes punched out of "
        "the background. The page to check against the drawing.",
    ),
    (
        "pdf/<file>_default.pdf",
        "Rapid-travel diagnostic per PLT (`--default-plots`): the toolpath "
        "ordering view of one output file.",
    ),
    (
        "pdf/[<plate>_][<material>_]all_<job_id>_default.pdf",
        "Combined rapid-travel diagnostic (`--default-plots`): strokes colored "
        "by cumulative travel distance, air moves dashed - the toolpath "
        "ordering view of the whole plate.",
    ),
    (
        "generation_report/report.json",
        "Machine-readable job report: resolved values, per-label findings, "
        "stage timings, output inventory.",
    ),
    (
        "generation_report/summary.txt",
        "Human-readable summary: status, counts, warnings/errors, findings, stage timings.",
    ),
)

#: Non-ASCII codepoints the xelatex default text font lacks, mapped to LaTeX
#: replacements. The corpus scan in tests/test_build_pipeline_pdf.py pins that
#: every character present in the included sources is covered.
LATEX_GLYPH_MAP: Dict[str, str] = {
    "\u21d2": r"$\Rightarrow$",  # RIGHTWARDS DOUBLE ARROW
    "\u21e6": r"$\Leftarrow$",  # LEFTWARDS DOUBLE ARROW
    "\u2192": r"$\rightarrow$",  # RIGHTWARDS ARROW
    "\u2190": r"$\leftarrow$",  # LEFTWARDS ARROW
    "\u2265": r"$\geq$",  # GREATER-THAN OR EQUAL TO
    "\u2264": r"$\leq$",  # LESS-THAN OR EQUAL TO
    "\u2260": r"$\neq$",  # NOT EQUAL TO
    "\u2208": r"$\in$",  # ELEMENT OF
    "\u221e": r"$\infty$",  # INFINITY
    "\u2248": r"$\approx$",  # ALMOST EQUAL TO
    "\u00b1": r"$\pm$",  # PLUS-MINUS SIGN
    "\u00d7": r"$\times$",  # MULTIPLICATION SIGN
    "\u00f7": r"$\div$",  # DIVISION SIGN
    "\u2212": r"$-$",  # MINUS SIGN
    "\u00b7": r"\textperiodcentered{}",  # MIDDLE DOT
    "\u2022": r"\textbullet{}",  # BULLET
    "\u2020": r"\textdagger{}",  # DAGGER
    "\u2021": r"\textdaggerdbl{}",  # DOUBLE DAGGER
    "\u00ab": r"\guillemotleft{}",  # LEFT-POINTING DOUBLE ANGLE QUOTES
    "\u00bb": r"\guillemotright{}",  # RIGHT-POINTING DOUBLE ANGLE QUOTES
    "\u2026": r"\ldots{}",  # HORIZONTAL ELLIPSIS
    "\u00b0": r"\textdegree{}",  # DEGREE SIGN
}

#: Sections the builder can emit, in document order (``--sections``).
SECTION_ORDER: Tuple[str, ...] = (
    "cover",
    "quickstart",
    "spec",
    "config",
    "topics",
    "examples",
    "gallery",
    "appendices",
)

QUICKSTART_MARKDOWN = """\
# Quick start

This handbook documents the **generation pipeline** (`plt-optimizer generate`):
the tool that turns a YAML job specification into engraver-ready per-cutter
HPGL/PLT files. Every number in this document is in **inches** - the tool
converts to plotter units (1016 units/inch, HPGL's 0.025 mm default step) at
emission, so never pre-scale anything.

## The five-step loop

1. **Copy a recipe** from `examples/job_specs/` (the recipes skill maps job
   shapes to starting points) and change `job_name` first - it seeds the output
   file names, so a new job must not overwrite an old run.
2. **Author the spec.** A job YAML is a single top-level `job:` mapping. Pick
   exactly one label source: an explicit `labels` list, a root-level `content`
   (+ optional `count`), or a job-level `replacement_text_file` data file.
3. **Validate before running:**

   ```bash
   uv run python scripts/validate_job_spec.py my_job.yaml
   ```

   A config defect is reported per spec, so validate the shop config first when
   it changed (`uv run python scripts/validate_job_config.py`).
4. **Run:**

   ```bash
   plt-optimizer generate my_job.yaml
   ```

5. **Check the proof** in `pdf/[<plate>_][<material>_]all_<job_id>.pdf` (one
   colored page per plate) and read `generation_report/summary.txt` for the
   findings (compression, descender relief, collision avoidance).

## CLI flags

| Flag | Meaning (default) |
|---|---|
| `-o`, `--output` | output directory (spec file's directory) |
| `--job-config` | shop defaults JSON (`job-config.json` in the CWD) |
| `--tools` | cutter inventory JSON (`tools.json` in the CWD) |
| `--fast-mode` | single travel strategy - faster, longer rapid travel |
| `--no-plots` | skip PDF previews |
| `--default-plots` | also write rapid-travel diagnostic PDFs |
| `-v`, `--verbose` | DEBUG logging |

## Non-negotiable invariants

- **All units are inches.** The device scale (1016 units/inch) is owned by
  `plt_optimizer/core/units.py`.
- **Unknown keys fail loudly** at every tier (`additionalProperties: false`): a
  typo aborts `parse_yaml` with its exact path (e.g.
  `labels.0.content.0.tex_height`). The free-form `tool_options` mapping is the
  one exception.
- **`null`/omitted = inherit; an explicit value always wins** at its tier.
  Precedence is job YAML > `job-config.json` > code fallback.
- **Exactly one label source per job.**
- **Determinism is a product requirement:** identical input + `job-config.json`
  + `tools.json` engraves identical output. Do not re-run unchanged inputs.
- **`job-config.json` is the shop's live defaults** and `tools.json` the
  physical cutter inventory: production runs must point at them.

## How to read the rest of this handbook

- **Part I** is the exhaustive `JobSpec` field reference (every model, type,
  default, constraint range, description) - the place to look up one field.
- **Part II** is the same treatment for the `job-config.json` shop tier.
- **Part III** is the semantics prose, topic by topic: the mechanisms the
  schema cannot express (cascade mechanics, kerning, collision avoidance,
  packing, every markup tag, validation, troubleshooting).
- **Part IV** shows complete, validated example jobs with their rendered proofs.
- **Part V** lists every file a run writes and what each is for.
"""


# --------------------------------------------------------------------------- #
# Small markdown utilities (pure; unit-tested)
# --------------------------------------------------------------------------- #

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n+", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*)$")
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block from a markdown document.

    Args:
        text: Raw markdown, optionally starting with a ``---`` delimited block.

    Returns:
        The document body with the frontmatter (and its delimiters) removed.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return text
    return text[match.end() :]


def split_html_comment(text: str) -> Tuple[str, str]:
    """Split a leading ``<!-- ... -->`` banner from a markdown document.

    The generated references open with an AUTO-GENERATED banner, which is
    generator housekeeping and noise in a printed handbook.

    Args:
        text: Raw markdown.

    Returns:
        Tuple of ``(banner, body)``; ``banner`` is empty when the document does
        not start with a comment.
    """
    stripped = text.lstrip()
    if not stripped.startswith("<!--"):
        return "", text
    end = stripped.find("-->")
    if end == -1:
        return "", text
    return stripped[: end + 3], stripped[end + 3 :].lstrip("\n")


def _heading_indices(lines: Sequence[str]) -> List[int]:
    """Return indices of ATX heading lines that sit outside fenced code blocks.

    Args:
        lines: The document split on newlines.

    Returns:
        Ascending list of heading line indices.
    """
    indices: List[int] = []
    fence: Optional[str] = None
    for i, line in enumerate(lines):
        marker = _FENCE_RE.match(line)
        if marker is not None:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif fence == token:
                fence = None
            continue
        if fence is None and _HEADING_RE.match(line):
            indices.append(i)
    return indices


def demote_headings(text: str, levels: int) -> str:
    """Deepen every ATX heading in a document by ``levels``.

    Args:
        text: Markdown document body (frontmatter already stripped).
        levels: Number of ``#`` to add to each heading (0 leaves it alone).

    Returns:
        The document with heading levels shifted, capped at ``######``.
    """
    if levels <= 0:
        return text
    lines = text.split("\n")
    for i in _heading_indices(lines):
        match = _HEADING_RE.match(lines[i])
        if match is None:  # pragma: no cover - _heading_indices guarantees one
            continue
        lines[i] = "#" * min(6, len(match.group(1)) + levels) + " " + match.group(2)
    return "\n".join(lines)


def first_heading(text: str) -> Optional[str]:
    """Return the title of a document's first ATX heading.

    Args:
        text: Markdown document body.

    Returns:
        The heading text, or ``None`` when the document has no heading.
    """
    lines = text.split("\n")
    for i in _heading_indices(lines):
        match = _HEADING_RE.match(lines[i])
        if match is not None:
            return match.group(2).strip()
    return None


def drop_first_heading(text: str) -> str:
    """Remove a document's first ATX heading line (the title is re-added).

    Args:
        text: Markdown document body.

    Returns:
        The body with the opening heading line removed.
    """
    lines = text.split("\n")
    indices = _heading_indices(lines)
    if not indices:
        return text
    del lines[indices[0]]
    return "\n".join(lines).lstrip("\n")


def anchor_id(kind: str, name: str) -> str:
    """Build a document anchor id for an included section.

    Args:
        kind: Section family (``part``, ``skill``, ``ref``, ``job``).
        name: Stable source name (skill directory, file stem, spec name).

    Returns:
        A pandoc-safe identifier.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{kind}-{slug}"


def rewrite_links(text: str, base_dir: Path, anchors: Dict[str, str]) -> str:
    """Rewrite relative markdown links for single-document output.

    Links into included sources become in-document anchors; links to files that
    are not part of the handbook (JSON schemas, scripts, sample YAML) become
    literal repo-relative paths, which is what a printed page needs.

    Args:
        text: Markdown body.
        base_dir: Directory relative paths resolve against.
        anchors: Map of absolute source path -> anchor id for included files.

    Returns:
        The body with links rewritten.
    """

    def _replace(match: re.Match[str]) -> str:
        bang, label, target = match.group(1), match.group(2), match.group(3)
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target) or target.startswith("#"):
            return match.group(0)
        if not (target.startswith("../") or target.startswith("./") or "/" in target):
            return f"{label} (`{target}`)"
        resolved = (base_dir / target).resolve()
        if str(resolved) in anchors:
            return f"{bang}[{label}](#{anchors[str(resolved)]})"
        try:
            shown = resolved.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            shown = target
        return f"{label} (`{shown}`)"

    return _LINK_RE.sub(_replace, text)


def fence_block(text: str, language: str) -> str:
    """Wrap ``text`` in a fenced code block with a safe fence length.

    Args:
        text: Verbatim content (YAML, JSON, plain text).
        language: Info string for the fence.

    Returns:
        The fenced block, newline-terminated.
    """
    longest = max((len(m.group(0)) for m in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text.rstrip()}\n{fence}\n"


def truncate_lines(text: str, limit: int) -> str:
    """Truncate a listing to ``limit`` lines with an explicit omission note.

    Args:
        text: File content.
        limit: Maximum number of lines to keep.

    Returns:
        The kept lines, plus a trailing note when lines were dropped.
    """
    lines = text.splitlines()
    if len(lines) <= limit:
        return text.rstrip()
    return "\n".join(lines[:limit]) + f"\n... ({len(lines) - limit} more lines omitted)"


def summarize_report(summary_text: str) -> str:
    """Extract the status/counts block of a ``generation_report/summary.txt``.

    Args:
        summary_text: The full report summary.

    Returns:
        The report header through the outputs listing, dropping the per-label
        findings and stage-timing blocks.
    """
    out: List[str] = []
    for line in summary_text.splitlines():
        if line.startswith("Findings"):
            break
        out.append(line)
    return "\n".join(out).rstrip()


def display_path(path: Path) -> str:
    """Return a path string usable as a pandoc resource reference.

    Args:
        path: Any filesystem path.

    Returns:
        The repo-relative POSIX path when the file lives in the checkout,
        otherwise the absolute POSIX path (the scratch workdir is normally a
        system temp directory outside the repo).
    """
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def safe_name(name: str) -> str:
    """Return a filesystem-safe form of an arbitrary name.

    Args:
        name: A spec stem, PDF stem, or other label.

    Returns:
        The name with every run of unsafe characters replaced by ``_``.
    """
    return _UNSAFE_RE.sub("_", name)


# --------------------------------------------------------------------------- #
# Source discovery
# --------------------------------------------------------------------------- #


def skill_order() -> List[str]:
    """Return sub-skill directory names in the main skill's TOC order.

    The TOC block of ``generate-engraving-job/SKILL.md`` is the canonical
    reading order (it is what the production agent sees), so the printed
    handbook follows it. Any skill directory missing from the TOC is appended
    alphabetically.

    Returns:
        Sub-skill directory names (the main skill excluded).
    """
    names: List[str] = []
    main = SKILL_ROOT / "generate-engraving-job" / "SKILL.md"
    if main.is_file():
        for match in re.finditer(r"\.\./([a-z0-9-]+)/SKILL\.md", main.read_text("utf-8")):
            name = match.group(1)
            if name not in names and (SKILL_ROOT / name).is_dir():
                names.append(name)
    for directory in sorted(p for p in SKILL_ROOT.iterdir() if p.is_dir()):
        if (
            directory.name != "generate-engraving-job"
            and (directory / "SKILL.md").is_file()
            and directory.name not in names
        ):
            names.append(directory.name)
    return names


@dataclass(frozen=True)
class TopicFile:
    """One reference file that belongs in a topic section."""

    path: Path
    title: str
    language: str = "markdown"


def topic_files(skill: str) -> List[TopicFile]:
    """List the reference files of one sub-skill in handbook order.

    Args:
        skill: Sub-skill directory name.

    Returns:
        ``semantics.md`` first, then the generated ``fallback-defaults.md``,
        then YAML snippets, then any other markdown - excluding the two
        references rendered as Parts I/II.
    """
    refs = SKILL_ROOT / skill / "references"
    if not refs.is_dir():
        return []
    found: List[TopicFile] = []
    for name, title, language in (
        ("semantics.md", "Semantics", "markdown"),
        ("fallback-defaults.md", "Fallback defaults", "markdown"),
        ("snippets.yaml", "YAML snippets", "yaml"),
    ):
        path = refs / name
        if path.is_file():
            found.append(TopicFile(path, title, language))
    for path in sorted(refs.glob("*.md")):
        if (
            path.name not in ("semantics.md", "fallback-defaults.md")
            and path.name not in PART_REFERENCES
        ):
            found.append(TopicFile(path, path.stem, "markdown"))
    return found


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #


def repo_version() -> str:
    """Return the package version for the cover page.

    Returns:
        ``plt_optimizer.__version__``, or ``"unknown"`` when unavailable.
    """
    try:
        import plt_optimizer

        return str(plt_optimizer.__version__)
    except Exception:  # pragma: no cover - the cover must always build
        return "unknown"


def git_commit() -> str:
    """Return the short HEAD commit hash for the cover page.

    Returns:
        The abbreviated commit, or ``"unknown"`` outside a git checkout.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return proc.stdout.strip()


def tool_versions() -> List[str]:
    """Collect the build toolchain versions for the cover page.

    Returns:
        One line per available tool (``pandoc``, ``xelatex``, ``gs``).
    """
    lines: List[str] = []
    for cmd in (["pandoc", "--version"], ["xelatex", "--version"]):
        if shutil.which(cmd[0]) is None:
            continue
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        lines.append((proc.stdout or proc.stderr).split("\n", 1)[0].strip())
    if shutil.which("gs") is not None:
        proc = subprocess.run(["gs", "--version"], capture_output=True, text=True, check=False)
        lines.append(f"ghostscript {proc.stdout.strip()}")
    return lines


def build_cover(sections: Sequence[str], dpi: int) -> str:
    """Render the cover/title page.

    Args:
        sections: Sections the build includes (listed for traceability).
        dpi: Rasterization density used for the previews.

    Returns:
        Markdown for the cover.
    """
    tools = "\n".join(f"- {line}" for line in tool_versions()) or "- (no PDF toolchain found)"
    sources = "\n".join(
        f"- `{display_path(path)}`"
        for path in sorted(p for p in SKILL_ROOT.rglob("*.md") if p.is_file())
    )
    return f"""# PLT-Optimizer Generation Pipeline Handbook

**Job spec YAML schema + `job-config.json` shop defaults + topic semantics +
worked examples with rendered proofs.**

| | |
|---|---|
| Tool | `plt-optimizer generate` (package `plt_optimizer`) |
| Version | {repo_version()} |
| Commit | `{git_commit()}` |
| Built | {dt.date.today().isoformat()} |
| Units | **inches** everywhere (device scale 1016 units/inch) |
| Previews | rasterized at {dpi} DPI |
| Sections | {", ".join(sections)} |

## Build toolchain

{tools}

## Sources included in this build

{sources}

*Generated by `scripts/build_pipeline_pdf.py`. Every field table is rendered
verbatim from the committed artifacts, which are themselves auto-generated from
the Pydantic models and drift-pinned by the test suite.*
"""


def include_reference(name: str, anchors: Dict[str, str]) -> str:
    """Inline one generated reference document.

    The body is included verbatim (minus the AUTO-GENERATED HTML banner) so the
    printed text stays identical to the committed artifact.

    Args:
        name: Reference file name (``JOB_SPEC.md`` / ``JOB_CONFIG.md``).
        anchors: Anchor registry, updated with this file's id.

    Returns:
        Markdown for the part body.
    """
    path = REFERENCE_ROOT / name
    anchors[str(path)] = anchor_id("part", name)
    body = strip_frontmatter(path.read_text(encoding="utf-8"))
    _banner, body = split_html_comment(body)
    # The part heading supplies the title; drop the document's own H1.
    body = drop_first_heading(body)
    return rewrite_links(body, path.parent, anchors).rstrip() + "\n"


def build_reference_section(ref: TopicFile, skill: str, anchors: Dict[str, str]) -> str:
    """Render one reference file as a subsection of its skill.

    Args:
        ref: The reference file.
        skill: Owning sub-skill directory name.
        anchors: Anchor registry, updated with this file's id.

    Returns:
        Markdown for the subsection.
    """
    anchor = anchor_id("ref", f"{skill}-{ref.path.stem}")
    anchors[str(ref.path)] = anchor
    if ref.language != "markdown":
        shown = display_path(ref.path)
        return f"### {ref.title} {{#{anchor}}}\n\nSource: `{shown}`.\n\n" + fence_block(
            ref.path.read_text(encoding="utf-8"), ref.language
        )
    body = strip_frontmatter(ref.path.read_text(encoding="utf-8"))
    _banner, body = split_html_comment(body)
    title = first_heading(body) or ref.title
    anchors[str(ref.path)] = anchor
    inner = demote_headings(drop_first_heading(body), 2)
    return (
        f"### {title} {{#{anchor}}}\n\n"
        + rewrite_links(inner, ref.path.parent, anchors).rstrip()
        + "\n"
    )


def build_topic_sections(anchors: Dict[str, str]) -> str:
    """Render Part III: every sub-skill with its references, in TOC order.

    Args:
        anchors: Anchor registry, updated with each included file's id.

    Returns:
        Markdown for the topics part body.
    """
    skills = ["generate-engraving-job"] + skill_order()
    # Pre-register every anchor so links that point FORWARD (the main skill's
    # TOC referencing a later sub-skill) resolve during the rewrite pass.
    for skill in skills:
        path = SKILL_ROOT / skill / "SKILL.md"
        if path.is_file():
            anchors[str(path)] = anchor_id("skill", skill)
        for ref in topic_files(skill):
            anchors[str(ref.path)] = anchor_id("ref", f"{skill}-{ref.path.stem}")

    chunks: List[str] = []
    for skill in skills:
        path = SKILL_ROOT / skill / "SKILL.md"
        if not path.is_file():
            continue
        body = strip_frontmatter(path.read_text(encoding="utf-8"))
        title = first_heading(body) or skill
        inner = demote_headings(drop_first_heading(body), 1)
        chunks.append(
            f"## {title} {{#{anchors[str(path)]}}}\n\n"
            + rewrite_links(inner, path.parent, anchors).rstrip()
            + "\n"
        )
        for ref in topic_files(skill):
            chunks.append(build_reference_section(ref, skill, anchors))
    return "\n".join(chunks)


# --------------------------------------------------------------------------- #
# Examples + gallery
# --------------------------------------------------------------------------- #


@dataclass
class JobOutput:
    """The artifacts one ``generate`` run produced."""

    job: ShowcaseJob
    out_dir: Optional[Path] = None
    plt_files: List[Path] = field(default_factory=list)
    pdf_files: List[Path] = field(default_factory=list)
    preview_images: List[Path] = field(default_factory=list)
    summary: str = ""
    error: str = ""


def spec_path(name: str) -> Path:
    """Resolve an example spec name to its path.

    Args:
        name: File name inside ``examples/job_specs/``.

    Returns:
        The absolute spec path.
    """
    return EXAMPLES_ROOT / name


def combined_proofs(pdf_files: Sequence[Path]) -> List[Path]:
    """Select the combined per-plate proofs from a run's previews.

    Args:
        pdf_files: Every PDF preview the run wrote.

    Returns:
        The ``all_*.pdf`` proofs, sorted; rapid-travel ``*_default.pdf``
        diagnostics are excluded (Part V shows those separately).
    """
    return sorted(
        p
        for p in pdf_files
        if not p.stem.endswith("_default") and (p.stem.startswith("all_") or "_all_" in p.stem)
    )


def proof_pages(out_dir: Path, stem: str) -> List[Path]:
    """Collect the rasterized pages of one proof, in page order.

    Args:
        out_dir: The directory :func:`rasterize_proofs` wrote into.
        stem: The source PDF stem (already passed through :func:`safe_name`).

    Returns:
        The matching PNG pages, sorted. The ``-page`` marker is what makes the
        glob unambiguous: a plain ``_{n}.png`` suffix would also match a
        longer-named proof's pages (``30_txt_Job`` vs ``30_txt_Job_default``).
    """
    return sorted(out_dir.glob(f"{stem}-page*.png"))


def rasterize_proofs(pdf_files: Sequence[Path], out_dir: Path, dpi: int) -> List[Path]:
    """Rasterize PDF proofs to PNG pages.

    Args:
        pdf_files: The PDFs to render (the combined proofs for example
            sections; every kind for the gallery).
        out_dir: Directory for the PNG pages.
        dpi: Rasterization density.

    Returns:
        The written PNG paths, in proof then page order.
    """
    images: List[Path] = []
    if not pdf_files:
        return images
    out_dir.mkdir(parents=True, exist_ok=True)
    for proof in sorted(pdf_files):
        stem = safe_name(proof.stem)
        # The "-page" marker keeps the collection glob from matching a
        # longer-named proof's pages (e.g. "30_txt_Job" vs
        # "30_txt_Job_default" both ending in "_<n>.png").
        pattern = out_dir / f"{stem}-page%d.png"
        proc = subprocess.run(
            [
                "gs",
                "-dNOPAUSE",
                "-dBATCH",
                "-dQUIET",
                "-dSAFER",
                "-sDEVICE=png16m",
                f"-r{dpi}",
                f"-sOutputFile={pattern}",
                str(proof),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            logger.warning("rasterize failed for %s: %s", proof, proc.stderr.strip()[:200])
            continue
        images.extend(proof_pages(out_dir, stem))
    return images


def run_generate(
    job: ShowcaseJob, workdir: Path, dpi: int, default_plots: bool = False
) -> JobOutput:
    """Generate one example spec and rasterize its combined proof.

    Args:
        job: The showcase entry.
        workdir: Directory to write the run's outputs under.
        dpi: Rasterization density for the previews.
        default_plots: Also request the rapid-travel diagnostic PDFs (the
            gallery run needs every artifact kind present).

    Returns:
        A populated :class:`JobOutput` (``error`` set when the run failed).
    """
    spec = spec_path(job.spec)
    out_dir = workdir / safe_name(spec.stem)
    cmd = ["uv", "run", "plt-optimizer", "generate", str(spec), "-o", str(out_dir)]
    if default_plots:
        cmd.append("--default-plots")
    logger.info("generate %s%s", job.spec, " (with --default-plots)" if default_plots else "")
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    result = JobOutput(job=job, out_dir=out_dir)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().split("\n")[-5:]
        result.error = f"exit {proc.returncode}: " + " | ".join(tail)
        logger.warning("generate failed for %s: %s", job.spec, result.error)
        return result
    result.plt_files = sorted((out_dir / "plt").glob("*.plt"))
    result.pdf_files = sorted((out_dir / "pdf").glob("*.pdf"))
    summary = out_dir / "generation_report" / "summary.txt"
    if summary.is_file():
        result.summary = summary.read_text(encoding="utf-8")
    result.preview_images = rasterize_proofs(
        combined_proofs(result.pdf_files), out_dir / "handbook", dpi
    )
    return result


def rehydrate(job: ShowcaseJob, workdir: Path, dpi: int) -> JobOutput:
    """Rebuild a :class:`JobOutput` from a previous run's workdir.

    Args:
        job: The showcase entry.
        workdir: The retained workdir containing the run's output.
        dpi: Rasterization density for missing previews.

    Returns:
        The rehydrated output (``error`` set when the run is not on disk).
    """
    out_dir = workdir / safe_name(spec_path(job.spec).stem)
    result = JobOutput(job=job, out_dir=out_dir)
    if not out_dir.is_dir():
        result.error = f"no previous output in {out_dir}"
        return result
    result.plt_files = sorted((out_dir / "plt").glob("*.plt"))
    result.pdf_files = sorted((out_dir / "pdf").glob("*.pdf"))
    summary = out_dir / "generation_report" / "summary.txt"
    if summary.is_file():
        result.summary = summary.read_text(encoding="utf-8")
    result.preview_images = rasterize_proofs(
        combined_proofs(result.pdf_files), out_dir / "handbook", dpi
    )
    return result


def _file_listing(output: JobOutput) -> str:
    """Render the bullet list of PLT/PDF files one run wrote.

    Args:
        output: The run result.

    Returns:
        Markdown bullets, or an empty string when nothing was recorded.
    """
    rows = [f"- `{p.name}`" for p in (*output.plt_files, *output.pdf_files)]
    if not rows:
        return ""
    return "\n".join(rows) + "\n"


def _preview_pages(output: JobOutput, images: Sequence[Path], height: str) -> str:
    """Render rasterized proof pages as markdown images.

    Args:
        output: The run result (supplies the example name for alt text).
        images: PNG pages to embed.
        height: LaTeX height attribute for each page.

    Returns:
        Markdown for the image block (empty when there are no pages).
    """
    body = [
        f"![Plate proof for `{output.job.spec}` ({image.name})]"
        f"({display_path(image)}){{width=100%,height={height}}}\n"
        for image in images
    ]
    return "\n".join(body)


def build_example_section(output: JobOutput) -> str:
    """Render one worked example (spec, data files, report, proof).

    Args:
        output: The run result for the example.

    Returns:
        Markdown for the example subsection.
    """
    job = output.job
    spec = spec_path(job.spec)
    anchor = anchor_id("job", spec.stem)
    chunks = [f"### {spec.stem} {{#{anchor}}}\n\n{job.blurb}\n\nSource: `{display_path(spec)}`.\n"]
    if not spec.is_file():
        chunks.append(f"\n> **Spec not found:** `{job.spec}`\n")
        return "".join(chunks)
    if output.out_dir is None:
        chunks.append("\n#### Job spec YAML\n\n" + fence_block(spec.read_text("utf-8"), "yaml"))
        chunks.append(
            "\n*This build skipped the generate runs (`--no-previews`), so no "
            "report or proof is shown for this example.*\n"
        )
        return "".join(chunks)
    if output.error:
        chunks.append(f"\n> **Generation failed during this build:** `{output.error}`\n")
        return "".join(chunks)

    chunks.append("\n#### Job spec YAML\n\n" + fence_block(spec.read_text("utf-8"), "yaml"))
    for name in job.data_files:
        data = EXAMPLES_ROOT / name
        if not data.is_file():
            chunks.append(f"\nMissing data file `{name}`.\n")
            continue
        text = truncate_lines(data.read_text("utf-8"), DATA_FILE_LINES)
        chunks.append(f"\n#### Data file `{name}`\n\n" + fence_block(text, "text"))
    if output.summary:
        chunks.append(
            "\n#### Generation report\n\n" + fence_block(summarize_report(output.summary), "text")
        )
    listing = _file_listing(output)
    if listing:
        chunks.append(f"\n#### Files written\n\n{listing}")
    pages = _preview_pages(output, output.preview_images, "86%")
    if pages:
        chunks.append(f"\n#### Rendered proof\n\n{pages}")
    return "".join(chunks)


def build_artifact_table() -> str:
    """Render the artifact-kind table of Part V.

    Returns:
        A markdown table with one row per artifact pattern.
    """
    rows = ["| Artifact | What it is |", "|---|---|"]
    rows.extend(f"| `{pattern}` | {role} |" for pattern, role in ARTIFACT_ROLES)
    return "\n".join(rows) + "\n"


def build_gallery_section(outputs: Sequence[JobOutput], dpi: int) -> str:
    """Render Part V: every artifact kind, with the gallery run's real files.

    Args:
        outputs: The gallery run(s) (runs whose spec is :data:`GALLERY_JOB`).
        dpi: Rasterization density for the preview pages.

    Returns:
        Markdown for the artifacts part body.
    """
    chunks = [build_artifact_table()]
    for output in outputs:
        if output.out_dir is None:
            continue
        if output.error:
            chunks.append(f"\n> Gallery run failed: `{output.error}`\n")
            continue
        files = sorted(
            p
            for p in output.out_dir.rglob("*")
            if p.is_file() and not any(part.startswith("handbook") for part in p.parts)
        )
        body = "\n".join(
            f"- `{p.relative_to(output.out_dir).as_posix()}` ({p.stat().st_size / 1024:.0f} KB)"
            for p in files
        )
        chunks.append(f"\n## Gallery run: `{GALLERY_JOB}`\n\n{body}\n")
        for pdf in output.pdf_files:
            images = rasterize_proofs([pdf], output.out_dir / "handbook-gallery", dpi)
            label = pdf.name
            chunks.append(f"\n### Preview: `{label}`\n\n" + _preview_pages(output, images, "78%"))
    return "\n".join(chunks)


def build_field_index() -> str:
    """Render the appendix index of field names to their declaring models.

    Returns:
        A markdown table, or a note when the schema is unavailable.
    """
    schema_path = REFERENCE_ROOT / "job_spec.schema.json"
    if not schema_path.is_file():
        return "*(the committed `job_spec.schema.json` was not found)*\n"
    schema = json.loads(schema_path.read_text("utf-8"))
    defs: Dict[str, Dict[str, Any]] = {
        k: dict(v) for k, v in (schema.get("$defs") or {}).items()
    }
    if isinstance(schema.get("properties"), dict):
        defs.setdefault("JobSpec", schema)
    owners: Dict[str, List[str]] = {}
    for model, body in defs.items():
        props = body.get("properties")
        if not isinstance(props, dict):
            continue
        for name in props:
            owners.setdefault(name, []).append(model)
    lines = []
    for name in sorted(owners):
        models = ", ".join(f"`{m}`" for m in sorted(owners[name]))
        lines.append(f"- `{name}` — {models}")
    return "\n".join(lines) + "\n"


def build_appendices() -> str:
    """Render the appendices (schema pointers + field index).

    Returns:
        Markdown for the appendices part.
    """
    sizes = ", ".join(
        f"`{display_path(p)}` ({p.stat().st_size // 1024} KB)"
        for p in (
            REFERENCE_ROOT / "job_spec.schema.json",
            REFERENCE_ROOT / "job_config.schema.json",
        )
        if p.is_file()
    )
    return f"""# Appendices

## Machine-readable JSON Schemas

The two schemas are the contracts that Parts I-II render from. They are
intentionally **not** reproduced page-for-page here: they carry the same
information as the field tables in a machine format, and dumping them would add
well over a thousand printed pages.

- {sizes}

`job_spec.schema.json` validates the parsed `job:` mapping of any spec YAML
(`plt_optimizer.generate.schema.parse_yaml` is the real gate; the schema exists
for editor tooling and structural checks). `job_config.schema.json` validates
`job-config.json` shop defaults - add it as the `"$schema"` key of a config file
to get per-field hover documentation in a JSON-aware editor.

## Field name index

Every field name in the `JobSpec` model family and the models that declare it
(cascade tiers share field names; this answers "where is this field defined?").

{build_field_index()}"""


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


@dataclass
class BuildPlan:
    """The resolved set of sections, jobs, and paths for one build."""

    sections: Tuple[str, ...]
    jobs: Tuple[ShowcaseJob, ...]
    out: Path
    workdir: Path
    dpi: int
    previews: bool
    keep_workdir: bool = False
    skip_generate: bool = False


def resolve_jobs(spec: str) -> Tuple[ShowcaseJob, ...]:
    """Resolve the ``--jobs`` selector to the showcase list to build.

    Args:
        spec: ``curated``, ``all`` (every spec in ``examples/job_specs/``), or a
            comma-separated list of spec file names.

    Returns:
        The jobs to feature, in output order.
    """
    known = {job.spec: job for job in SHOWCASE_JOBS}
    if spec == "curated":
        return SHOWCASE_JOBS
    if spec == "all":
        return tuple(
            known.get(path.name, ShowcaseJob(path.name, "Production example job."))
            for path in sorted(EXAMPLES_ROOT.glob("*.yaml"))
        )
    names = [n.strip() for n in spec.split(",") if n.strip()]
    return tuple(known.get(n, ShowcaseJob(n, "Custom example.")) for n in names)


def resolve_sections(spec: str) -> Tuple[str, ...]:
    """Resolve the ``--sections`` selector.

    Args:
        spec: ``all`` or a comma-separated list of section names.

    Returns:
        The requested sections, in canonical document order.

    Raises:
        ValueError: When a requested section name is unknown.
    """
    if spec == "all":
        return SECTION_ORDER
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in SECTION_ORDER]
    if unknown:
        raise ValueError(
            f"unknown section(s): {', '.join(unknown)}; valid: {', '.join(SECTION_ORDER)}"
        )
    return tuple(s for s in SECTION_ORDER if s in wanted)


def assemble(plan: BuildPlan, outputs: Sequence[JobOutput]) -> str:
    """Assemble the master markdown document.

    Args:
        plan: The resolved build plan.
        outputs: Generated example outputs, keyed by ``job.spec``.

    Returns:
        The complete master markdown.
    """
    anchors: Dict[str, str] = {}
    parts: List[str] = []
    if "cover" in plan.sections:
        parts.append(build_cover(plan.sections, plan.dpi))
    if "quickstart" in plan.sections:
        parts.append(QUICKSTART_MARKDOWN)
    if "spec" in plan.sections:
        parts.append("# Part I - Job Spec Reference\n\n")
        parts.append(include_reference("JOB_SPEC.md", anchors))
    if "config" in plan.sections:
        parts.append("# Part II - Shop Config Reference\n\n")
        parts.append(include_reference("JOB_CONFIG.md", anchors))
    if "topics" in plan.sections:
        parts.append(
            "# Part III - Semantics by Topic\n\n"
            "The mechanisms a schema cannot express. Sections follow the main "
            "skill's table-of-contents order; each pairs a skill's own page "
            "with its curated semantics reference.\n\n"
        )
        parts.append(build_topic_sections(anchors))
    if "examples" in plan.sections:
        parts.append(
            "# Part IV - Worked Examples\n\n"
            "Complete, validated specs from `examples/job_specs/`, each shown "
            "with its data files, the generation report it produced, and the "
            "rendered plate proof.\n\n"
        )
        by_name = {o.job.spec: o for o in outputs}
        for job in plan.jobs:
            parts.append(build_example_section(by_name.get(job.spec, JobOutput(job=job))))
    if "gallery" in plan.sections:
        gallery = [o for o in outputs if o.job.spec == GALLERY_JOB]
        parts.append(
            "# Part V - Output Artifacts\n\n"
            "Every file a `plt-optimizer generate` run writes, shown for "
            f"`{GALLERY_JOB}` run with `--default-plots` so all preview kinds "
            "are present.\n\n"
        )
        parts.append(build_gallery_section(gallery, plan.dpi))
    if "appendices" in plan.sections:
        parts.append(build_appendices())
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# External steps: latex header, pandoc
# --------------------------------------------------------------------------- #


def write_latex_header(path: Path) -> None:
    """Write the LaTeX preamble that maps the corpus's exotic glyphs.

    Args:
        path: Destination ``.tex`` fragment (pandoc ``--include-in-header``).
    """
    lines = [r"\usepackage{newunicodechar}", r"\usepackage{longtable}"]
    lines.extend(
        rf"\newunicodechar{{{char}}}{{{replacement}}}"
        for char, replacement in sorted(LATEX_GLYPH_MAP.items())
    )
    lines.append(r"\setlength{\LTpre}{0pt}")
    lines.append(r"\setlength{\LTpost}{0pt}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


PANDOC_ARGS: Tuple[str, ...] = (
    "--standalone",
    "--toc",
    "--toc-depth=2",
    "--number-sections",
    "--pdf-engine=xelatex",
    "-V",
    "documentclass=article",
    "-V",
    "papersize=letter",
    "-V",
    "geometry:landscape",
    "-V",
    "geometry:margin=1.5cm",
    "-V",
    "fontsize=10pt",
    "-V",
    "colorlinks=true",
    "-V",
    "linkcolor=blue!55!black",
    "-V",
    "urlcolor=blue!55!black",
    "-V",
    "toccolor=blue!55!black",
)


def pandoc_command(master: Path, header: Path, out: Path) -> List[str]:
    """Build the pandoc argv for one PDF render.

    Args:
        master: The assembled master markdown.
        header: The LaTeX header fragment.
        out: Destination PDF.

    Returns:
        The argv list (run with the repo root as CWD so resource paths resolve).
    """
    return [
        "pandoc",
        str(master),
        "-o",
        str(out),
        f"--include-in-header={header}",
        f"--resource-path={REPO_ROOT}",
        f"--resource-path={master.parent}",
        *PANDOC_ARGS,
    ]


def preflight(need_gs: bool) -> List[str]:
    """Check the external build tools.

    Args:
        need_gs: Whether ghostscript is required for this build.

    Returns:
        The missing tool names (empty when everything is available).
    """
    required = ["pandoc", "xelatex"] + (["gs"] if need_gs else [])
    return [name for name in required if shutil.which(name) is None]


def count_pdf_pages(path: Path) -> Optional[int]:
    """Count the pages of a PDF using ghostscript.

    Args:
        path: The PDF to inspect.

    Returns:
        The page count, or ``None`` when it cannot be determined.
    """
    if shutil.which("gs") is None:
        return None
    proc = subprocess.run(
        ["gs", "-dNOPAUSE", "-dBATCH", "-sDEVICE=nullpage", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return len(re.findall(r"^Page", proc.stdout, re.MULTILINE)) or None


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command line arguments.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(
        description="Build the generate-pipeline PDF handbook from repo docs.",
    )
    parser.add_argument("-o", "--out", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="Scratch directory for generate runs + images (default: temp).",
    )
    parser.add_argument(
        "--keep-workdir", action="store_true", help="Do not delete the scratch dir."
    )
    parser.add_argument("--sections", default="all", help=f"all | {','.join(SECTION_ORDER)}")
    parser.add_argument("--jobs", default="curated", help="curated | all | <a.yaml,b.yaml>")
    parser.add_argument(
        "--previews",
        dest="previews",
        action="store_true",
        default=True,
        help="Run the examples and embed rasterized proofs (default).",
    )
    parser.add_argument(
        "--no-previews",
        dest="previews",
        action="store_false",
        help="Skip the generate runs (fast text-only build).",
    )
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument(
        "--skip-generate",
        action="store_true",
        help="Reuse a --keep-workdir run's output instead of regenerating.",
    )
    parser.add_argument(
        "--markdown-only",
        action="store_true",
        help="Write the assembled master markdown and stop (no PDF toolchain).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Build the handbook.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code (0 success, 2 bad args, 3 missing tools, 4 pandoc
        failure, 5 missing glyphs).
    """
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    try:
        sections = resolve_sections(args.sections)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2
    jobs = resolve_jobs(args.jobs)
    out: Path = args.out if args.out.is_absolute() else REPO_ROOT / args.out
    workdir: Path = args.workdir or Path(tempfile.mkdtemp(prefix="plt-handbook-"))
    workdir = workdir if workdir.is_absolute() else REPO_ROOT / workdir
    workdir.mkdir(parents=True, exist_ok=True)

    plan = BuildPlan(
        sections=sections,
        jobs=jobs,
        out=out,
        workdir=workdir,
        dpi=args.dpi,
        previews=args.previews,
        keep_workdir=args.keep_workdir,
        skip_generate=args.skip_generate,
    )

    outputs: List[JobOutput] = []
    wants_examples = "examples" in sections or "gallery" in sections
    if wants_examples and plan.previews:
        for job in jobs:
            # The gallery job is generated with --default-plots so Part V can
            # show every artifact kind the pipeline writes.
            gallery_extra = "gallery" in sections and job.spec == GALLERY_JOB
            outputs.append(
                rehydrate(job, workdir, plan.dpi)
                if plan.skip_generate
                else run_generate(job, workdir, plan.dpi, default_plots=gallery_extra)
            )
        if "gallery" in sections and not any(j.spec == GALLERY_JOB for j in jobs):
            gallery = next(j for j in SHOWCASE_JOBS if j.spec == GALLERY_JOB)
            outputs.append(run_generate(gallery, workdir, plan.dpi, default_plots=True))
    elif wants_examples:
        outputs = [JobOutput(job=job) for job in jobs]

    master = assemble(plan, outputs)
    master_path = workdir / "handbook.md"
    master_path.write_text(master, encoding="utf-8")
    logger.info("master markdown: %s (%d KB)", master_path, len(master) // 1024)

    if args.markdown_only:
        print(master_path)
        return 0

    missing = preflight(need_gs=wants_examples and plan.previews)
    if missing:
        logger.error(
            "missing build tool(s): %s - install pandoc, texlive and ghostscript, "
            "or use --markdown-only",
            ", ".join(missing),
        )
        return 3

    header = workdir / "handbook-header.tex"
    write_latex_header(header)
    out.parent.mkdir(parents=True, exist_ok=True)
    logger.info("pandoc -> %s", out)
    proc = subprocess.run(
        pandoc_command(master_path, header, out),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    for line in (proc.stdout or "").strip().split("\n"):
        if line.strip():
            logger.warning("pandoc: %s", line.strip())
    if proc.returncode != 0:
        for line in (proc.stderr or "").strip().split("\n")[-25:]:
            logger.error("pandoc: %s", line)
        return 4
    if "Missing character" in (proc.stdout or ""):
        logger.error("xelatex reported missing glyphs - extend LATEX_GLYPH_MAP")
        return 5

    pages = count_pdf_pages(out)
    logger.info(
        "wrote %s (%s pages, %.1f MB)",
        out,
        pages if pages else "?",
        out.stat().st_size / (1024 * 1024),
    )
    if not plan.keep_workdir:
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
