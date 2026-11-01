"""Markdown-safety lint for rendered prose.

Field descriptions, generator notes and skill-tree prose all render as
markdown (JSON Schema hovers, ``JOB_SPEC.md`` / ``JOB_CONFIG.md``, the
sub-skill ``SKILL.md`` files). Bare ``<sup>``-style markup is swallowed as
HTML by the renderer, bare formulas do not read as math, and bare
``snake_case`` field names do not stand out as code. This lint pins the
convention:

- markup tags and placeholder chevrons -> backticks (`` `<sup>` ``);
- formulas -> ``$...$`` (``$1 - d/2$``) or backticks;
- every multi-word schema/job-config field named in prose -> backticks;
- code-shaped asterisk globs (``*_bh_*``, ``*.ttf``) -> backticks (intentional
  ``*emphasis*`` is exempt).

Scanned sources: ``Field(description=...)`` in ``schema.py`` /
``job_config.py``, ``_FALLBACK_NOTES`` in ``generate_ai_docs.py``, the
Theme ``core``/``use_when``/``pitfalls`` registry strings in
``generate_skills.py``, and every ``docs/skills/engraving-jobs/**/*.md``
body line (frontmatter and ``` fenced blocks excluded - frontmatter
``Keywords:`` lists are bare search terms by design, and fenced YAML
examples engrave tags literally).

The field set is derived from the committed JSON Schema artifacts, so
retuning ``job-config.json`` values can never break this test (config-value
immunity).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import List, Set, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_MODEL_FILES = (
    REPO_ROOT / "plt_optimizer" / "generate" / "schema.py",
    REPO_ROOT / "plt_optimizer" / "generate" / "job_config.py",
)
_GENERATOR_FILES = (
    REPO_ROOT / "docs" / "schema" / "generate_ai_docs.py",
    REPO_ROOT / "docs" / "schema" / "generate_skills.py",
)
_SKILL_TREE = REPO_ROOT / "docs" / "skills" / "engraving-jobs"
_SCHEMA_DIR = _SKILL_TREE / "engraving-job-complete-reference" / "references"

#: `<tag>`, `</tag>`, `<name:arg>` shaped text (markup tags / placeholders).
_TAG_RE = re.compile(r"<(/?[a-zA-Z][a-zA-Z0-9_/]*(?::[^>\s]*)?)>")
#: Formula shapes that must sit in `$...$` (or backticks) to read as math.
_MATH_RE = re.compile(
    r"(1 - d/2|(?<![\w/])d/2(?![\w])|1 - ratio|fraction \* cap height"
    r"|fraction \* text_height|N \* text_height|x' = x"
    r"|floor \* \(1 \+ clearance\)|height × tan|N × text_height)"
)
#: Inline code (single- or double-backtick) and math ($...$) spans.
#: The delimiter-run guards keep `` `` `x` `` `` (a double-backtick span
#: wrapping a single-backtick span) from mispairing across boundaries.
_SPAN_RE = re.compile(
    r"(?<!`)``(?!`)[^`]+(?<!`)``(?!`)"  # ``code``
    r"|(?<!`)`(?!`)[^`]+(?<!`)`(?!`)"  # `code`
    r"|\$[^$\n]+\$"  # $math$
)
#: Asterisk-wrapped tokens that are code, not emphasis: globs containing
#: underscores (`*_bh_*`) and extension globs (`*.ttf`, `*.json`). Bare,
#: they italicize in markdown; intentional `*word*` emphasis is exempt.
_GLOB_RE = re.compile(r"(?<![\w`$*])\*(?:[\w.]*_[\w.]*|\.[\w*]+)\*(?![\w*])")


def _span_mask(text: str) -> List[bool]:
    """Mark every character that sits inside a code/math span.

    Args:
        text: Raw prose text.

    Returns:
        Per-character booleans (True = inside a span, exempt from checks).
    """
    mask = [False] * len(text)
    for match in _SPAN_RE.finditer(text):
        for i in range(match.start(), match.end()):
            mask[i] = True
    return mask


def _violations(text: str, base_line: int = 1) -> List[Tuple[int, str, str]]:
    """Find bare tags / math / field names / globs in one prose string.

    Matches inside backtick/`$...$` spans are exempt; a match's line is
    attributed against ``base_line`` so multi-line blocks report the right
    line.

    Args:
        text: Raw prose (may contain newlines).
        base_line: Line number of the text's first line.

    Returns:
        List of (line, kind, offending snippet) triples.
    """
    mask = _span_mask(text)
    found: List[Tuple[int, str, str]] = []

    def emit(rx: re.Pattern, kind: str) -> None:
        for match in rx.finditer(text):
            if any(mask[i] for i in range(match.start(), match.end())):
                continue
            line = base_line + text.count("\n", 0, match.start())
            found.append((line, kind, match.group(0)))

    emit(_TAG_RE, "bare markup tag")
    emit(_MATH_RE, "bare formula")
    emit(_FIELD_RE, "bare field name")
    emit(_GLOB_RE, "bare glob")
    return found


def _field_names() -> Set[str]:
    """Collect multi-word field names from the committed JSON Schemas.

    Single-word fields (``text``, ``height``, ``width``) are excluded: in
    prose they are indistinguishable from English.

    Returns:
        The set of underscored property names across both schema documents.
    """
    names: Set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    names.update(name for name in value if "_" in name)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for doc in ("job_spec.schema.json", "job_config.schema.json"):
        walk(json.loads((_SCHEMA_DIR / doc).read_text(encoding="utf-8")))
    return names


_FIELD_RE = re.compile(
    r"(?<![\w`$])("
    + "|".join(sorted((re.escape(n) for n in _field_names()), key=len, reverse=True))
    + r")(?![\w`$])"
)


def _joined_text(node: ast.AST) -> str:
    """Concatenate the literal string parts of a prose expression.

    Handles implicit concatenation (one Constant), f-strings (FormattedValue
    placeholders contribute nothing), ``+`` joins (e.g. ``PLATE_PARITY_TAIL``
    name references contribute nothing) and tuples of phrases.

    Args:
        node: The AST node to flatten.

    Returns:
        The joined literal text (empty when the node carries no strings).
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else ""
    if isinstance(node, ast.JoinedStr):
        return "".join(_joined_text(value) for value in node.values)
    if isinstance(node, ast.BinOp):
        return _joined_text(node.left) + _joined_text(node.right)
    if isinstance(node, ast.Tuple):
        return "\n".join(_joined_text(element) for element in node.elts)
    return ""


def _py_prose(path: Path) -> List[Tuple[int, str]]:
    """Extract the audited prose strings from a generator/model source file.

    Args:
        path: The ``.py`` file to parse.

    Returns:
        (line number, text) pairs for Field descriptions, ``_FALLBACK_NOTES``
        values and Theme ``core``/``use_when``/``pitfalls`` strings.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prose: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "description":
            prose.append((node.lineno, _joined_text(node.value)))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            named = any(isinstance(t, ast.Name) and t.id == "_FALLBACK_NOTES" for t in targets)
            if named and isinstance(node.value, ast.Dict):
                for value in node.value.values:
                    prose.append((value.lineno, _joined_text(value)))
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Theme":
            for kw in node.keywords:
                if kw.arg in {"core", "use_when", "pitfalls"}:
                    prose.append((kw.value.lineno, _joined_text(kw.value)))
    return [(line, text) for line, text in prose if text]


def _md_prose(path: Path) -> str:
    """Extract the audited body text from a markdown file.

    Frontmatter and ``` fenced blocks are excluded (frontmatter keyword
    lists are bare by design; fenced YAML examples engrave tags literally).

    Args:
        path: The ``.md`` file to split.

    Returns:
        Body text with exempt regions blanked (line numbers preserved).
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    out: List[str] = []
    fence = False
    for i in range(start):
        out.append("")  # frontmatter: blanked, numbering preserved
    for i in range(start, len(lines)):
        if lines[i].strip().startswith("```"):
            fence = not fence
            out.append("")
            continue
        out.append("" if fence else lines[i])
    return "\n".join(out)


def _report(source: str, prose: List[Tuple[int, str]]) -> List[str]:
    """Collect readable violation lines for one source's prose strings.

    Args:
        source: Label for assertion messages (file path).
        prose: (base line, text) pairs to check.

    Returns:
        One message per violation ("file:line: kind `snippet`").
    """
    problems: List[str] = []
    for base_line, text in prose:
        problems += [
            f"{source}:{line}: {kind} `{snippet}`"
            for line, kind, snippet in _violations(text, base_line)
        ]
    return problems


_PY_SOURCES = (
    *_MODEL_FILES,
    *_GENERATOR_FILES,
)


class TestPythonProse:
    """Model descriptions and generator prose must be markdown-safe."""

    @pytest.mark.parametrize(
        "path",
        _PY_SOURCES,
        ids=[p.name for p in _PY_SOURCES],
    )
    def test_descriptions_and_notes_are_markdown_safe(self, path: Path) -> None:
        """No bare tags, bare formulas, or bare field names in py prose."""
        problems = _report(str(path.relative_to(REPO_ROOT)), _py_prose(path))
        assert not problems, "prose breaks markdown rendering:\n" + "\n".join(problems)


class TestSkillTreeProse:
    """Every generated/curated skill markdown body must be markdown-safe."""

    def test_markdown_bodies_are_clean(self) -> None:
        """No bare tags, bare formulas, or bare field names outside fences."""
        problems: List[str] = []
        for path in sorted(_SKILL_TREE.rglob("*.md")):
            source = str(path.relative_to(REPO_ROOT))
            problems += [
                f"{source}:{line}: {kind} `{snippet}`"
                for line, kind, snippet in _violations(_md_prose(path))
            ]
        assert not problems, "skill prose breaks markdown rendering:\n" + "\n".join(problems)


class TestLintSelfCheck:
    """The lint itself must fire on planted violations and pass fixes."""

    def test_detects_bare_tag(self) -> None:
        """A bare `<sup>` is a violation; the backticked form is clean."""
        assert (1, "bare markup tag", "<sup>") in _violations("a bare <sup> run")
        assert not _violations("a bare `<sup>` run")

    def test_detects_bare_math(self) -> None:
        """`1 - d/2` needs `$...$`; the delimited form is clean."""
        assert (1, "bare formula", "1 - d/2") in _violations("rises 1 - d/2 cap heights")
        assert not _violations("rises $1 - d/2$ cap heights")
        assert not _violations("rises `1 - d/2` cap heights")

    def test_detects_bare_field(self) -> None:
        """A real field name needs backticks; the backticked form is clean."""
        assert (1, "bare field name", "text_height") in _violations("set text_height to 0.5")
        assert not _violations("set `text_height` to 0.5")

    def test_detects_bare_glob(self) -> None:
        """A `*_bh_*`-style glob italicizes; the backticked form is clean."""
        assert (1, "bare glob", "*_bh_*") in _violations("the *_bh_* file name")
        assert not _violations("the `*_bh_*` file name")

    def test_intentional_emphasis_is_exempt(self) -> None:
        """`*word*` emphasis (no underscore/dot inside) is not a violation."""
        assert not _violations("a *recessed* (negative) penetration")

    def test_single_word_fields_are_not_enforced(self) -> None:
        """Prose words `text`/`height` must not be flagged as field names."""
        assert not _violations("the rendered text height")

    def test_double_backtick_span_is_exempt(self) -> None:
        """`` `` `field` `` `` (nested span markup) is not a violation."""
        assert not _violations("e.g. `` `max_h_compress` ``). Every model")

    def test_multi_line_span_is_exempt(self) -> None:
        """A code span opened on one line exempts the continuation line."""
        text = "(`counter_prefix + format_counter(k, counter_style) +\ncounter_suffix`) for k"
        assert not _violations(text)
