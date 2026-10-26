"""Drift + integrity tests for the generated Hermes skill tree (docs/skills/).

Guards the contract between the Pydantic models / theme registry and the
committed skill files so the production agent can never be handed a stale,
unreachable, or field-lying reference:

- every generated file (sub-skill ``SKILL.md``, ``references/snippets.yaml``,
  ``references/fallback-defaults.md``, ``recipes/SKILL.md`` +
  ``examples/*.yaml``, the main-skill AUTO-TOC block) must byte-match a fresh
  render of ``docs/schema/generate_skills.py`` (the live-from-config
  tool-options table region is excluded, mirroring the ``JOB_SPEC.md``
  config-value immunity pin);
- the theme registry must partition EVERY job-spec schema field exactly once;
- the main-skill TOC must list exactly the skill directories on disk;
- every file under the category must be referenced by name from a
  ``SKILL.md`` (Hermes installs only referenced files) and every link resolve;
- curated prose (``references/semantics.md``, the main skill, registry
  strings) may only backtick real schema fields, tool options, job-config
  keys, and allowlisted runtime identifiers;
- every embedded YAML fragment is valid + field-real; every recipe parses
  through ``parse_yaml``.

Regenerate with::

    uv run python docs/schema/generate_skills.py
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs" / "schema"

# Hermes advisory soft cap on a SKILL.md body (skill_view loads the whole
# file into context; oversized bodies warn at ~24k chars).
BODY_CHAR_LIMIT = 24_000


def _load_skills_generator() -> Any:
    """Import ``docs/schema/generate_skills.py`` as a module by path.

    Returns:
        The loaded generator module.
    """
    spec = importlib.util.spec_from_file_location(
        "generate_skills", DOCS_DIR / "generate_skills.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the module uses @dataclass, whose annotation
    # resolution looks the module up in sys.modules.
    sys.modules["generate_skills"] = module
    spec.loader.exec_module(module)
    return module


gen = _load_skills_generator()

# Single source of truth for the tree layout: the generator's own constants.
CATEGORY_DIR = gen.CATEGORY_DIR
REFERENCE_SKILL_DIR = gen.REFERENCE_SKILL_DIR
REFERENCE_SKILL_REFERENCES = gen.REFERENCE_SKILL_REFERENCES


def _schema() -> Dict[str, Any]:
    """Load the committed job-spec JSON Schema.

    Returns:
        The parsed schema document.
    """
    data: Dict[str, Any] = json.loads(
        (REFERENCE_SKILL_REFERENCES / "job_spec.schema.json").read_text(encoding="utf-8")
    )
    return data


def _skill_dirs() -> List[Path]:
    """List the category's skill directories (dirs containing a SKILL.md).

    Returns:
        Sorted skill directories.
    """
    return sorted(d for d in CATEGORY_DIR.iterdir() if d.is_dir() and (d / "SKILL.md").exists())


def _frontmatter(skill_md: Path) -> Dict[str, Any]:
    """Parse a SKILL.md's YAML frontmatter.

    Args:
        skill_md: Path to the SKILL.md.

    Returns:
        The frontmatter mapping.
    """
    text = skill_md.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{skill_md}: missing frontmatter"
    end = text.index("\n---\n", 4)
    data: Dict[str, Any] = yaml.safe_load(text[4 : end + 1])
    return data


def _allowed_tokens() -> Set[str]:
    """Build the set of snake_case identifiers prose may name.

    Returns:
        Schema fields + job-config keys + tool-option names + allowlist.
    """
    allowed = set(gen.all_field_names(_schema()))
    config = json.loads(
        (REFERENCE_SKILL_REFERENCES / "job_config.schema.json").read_text(
            encoding="utf-8"
        )
    )
    allowed |= set(config.get("properties", {}))
    allowed |= set(gen._ai._load_tool_options())
    allowed |= _EXTRA_ALLOWED
    return allowed


#: Runtime identifiers prose may name that are not schema fields.
_EXTRA_ALLOWED = {
    "parse_yaml",  # validation entry point
    "expand_job_spec",
    "resolve_job_spec",
    "available_cutters",  # tools.json keys
    "boundary_hole_cutter_size",
    "plate_width",
    "plate_height",
    "hole_diameter",
    "stroke_count",  # font catalog keys
    "font_name",
    "pass_spacing",  # boldness-ladder term (field descriptions)
    "job_id",  # output naming
    "tex_height",  # intentional typo example (schema descriptions)
    "cutting_speed",  # intentional typo example (tool-options semantics)
    "data_a",  # example data filename
    "cutter_diameter",  # resolved-line attribute (bold-font mechanism)
    "borders_holes",  # tool-option dual-default layer name
    "label_1",  # example label id
}

#: `badge_0000`-style synthesized label ids are legal prose tokens.
_ID_SUFFIX_RE = re.compile(r"_[0-9]{4}$")
_SNAKE_RE = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")


# ---------------------------------------------------------------------------
# Generated-file drift (--check semantics, in-process)
# ---------------------------------------------------------------------------


class TestGeneratedArtifacts:
    """Committed skill files must byte-match a fresh render."""

    def test_check_mode_clean(self) -> None:
        """Every generated file matches the registry render (CI drift pin)."""
        stale = gen.check_all(gen.build_file_plan())
        names = [str(p.relative_to(REPO_ROOT)) for p in stale]
        assert not stale, f"stale skill files; run generate_skills.py: {names}"

    def test_semantics_files_exist(self) -> None:
        """Every sub-skill has curated references/semantics.md.

        The main skill is exempt: its prose is the TOC-bearing SKILL.md.
        The complete-reference skill is exempt: it is fully generated
        (its references/ holds the generate_ai_docs artifacts).
        """
        exempt = {"generate-engraving-job", REFERENCE_SKILL_DIR.name}
        missing = [
            str(d.relative_to(REPO_ROOT))
            for d in _skill_dirs()
            if d.name not in exempt and not (d / "references" / "semantics.md").exists()
        ]
        assert not missing, f"skills without semantics.md: {missing}"

    def test_no_stub_semantics(self) -> None:
        """No semantics.md is left as a generator stub."""
        stubs = [
            str(path.relative_to(REPO_ROOT))
            for path in CATEGORY_DIR.glob("*/references/semantics.md")
            if "TODO" in path.read_text(encoding="utf-8")
        ]
        assert not stubs, f"stub semantics files: {stubs}"


# ---------------------------------------------------------------------------
# Registry partition + fragment/recipe validity
# ---------------------------------------------------------------------------


class TestRegistry:
    """The theme registry is a sound, complete partition of the schema."""

    def test_registry_problems_none(self) -> None:
        """Coverage (exact partition), fragments, and recipes are all clean."""
        assert gen.registry_problems() == []

    def test_every_schema_field_covered_exactly_once(self) -> None:
        """Each property of each model appears in exactly one theme."""
        counts: Dict[str, int] = {}
        for theme in gen.THEMES:
            for name in theme.fields:
                counts[name] = counts.get(name, 0) + 1
        fields = gen.all_field_names(_schema())
        assert set(counts) == fields, (
            f"uncovered: {sorted(fields - set(counts))}; unknown: {sorted(set(counts) - fields)}"
        )
        duplicated = sorted(name for name, count in counts.items() if count > 1)
        assert not duplicated, f"fields claimed by multiple themes: {duplicated}"

    def test_recipes_examples_match_generated_files(self) -> None:
        """recipes/examples/ contains exactly the registry's recipe files."""
        expected = {gen.recipe_slug(r.source) for r in gen.RECIPES}
        actual = {p.name for p in (CATEGORY_DIR / "recipes" / "examples").iterdir()}
        assert actual == expected

    def test_recipes_parse(self) -> None:
        """Every recipe parses through parse_yaml."""
        assert gen.recipe_problems() == []


# ---------------------------------------------------------------------------
# Main-skill TOC
# ---------------------------------------------------------------------------


class TestMainSkillToc:
    """The AUTO-TOC block lists exactly the skill directories on disk."""

    def test_markers_present(self) -> None:
        """The main skill carries exactly one well-formed marker pair."""
        text = (CATEGORY_DIR / "generate-engraving-job" / "SKILL.md").read_text(encoding="utf-8")
        assert text.count(gen.TOC_BEGIN) == 1
        assert text.count(gen.TOC_END) == 1
        assert text.index(gen.TOC_BEGIN) < text.index(gen.TOC_END)

    def test_toc_lists_every_skill_dir(self) -> None:
        """TOC links == skill dirs on disk (minus the main skill itself)."""
        text = (CATEGORY_DIR / "generate-engraving-job" / "SKILL.md").read_text(encoding="utf-8")
        block = text[text.index(gen.TOC_BEGIN) : text.index(gen.TOC_END)]
        linked = set(re.findall(r"\]\(\.\./([a-z0-9-]+)/SKILL\.md\)", block))
        on_disk = {d.name for d in _skill_dirs()} - {"generate-engraving-job"}
        assert linked == on_disk, (
            f"TOC missing: {sorted(on_disk - linked)}; TOC stale: {sorted(linked - on_disk)}"
        )


# ---------------------------------------------------------------------------
# Frontmatter + body size
# ---------------------------------------------------------------------------

_ALL_SKILLS = sorted(CATEGORY_DIR.glob("*/SKILL.md"))


class TestSkillFrontmatter:
    """Hermes frontmatter shape on every skill."""

    @pytest.mark.parametrize("skill_md", _ALL_SKILLS, ids=lambda p: str(p.parent.name))
    def test_frontmatter_shape(self, skill_md: Path) -> None:
        """name matches the dir; description carries triggers; category set."""
        data = _frontmatter(skill_md)
        assert data["name"] == skill_md.parent.name
        description = data["description"]
        assert isinstance(description, str) and len(description) > 20
        assert "Use when:" in description and "Keywords:" in description
        assert data["metadata"]["hermes"]["category"] == "engraving-jobs"

    @pytest.mark.parametrize("skill_md", _ALL_SKILLS, ids=lambda p: str(p.parent.name))
    def test_body_within_hermes_limit(self, skill_md: Path) -> None:
        """skill_view loads the whole body; keep it under the soft cap."""
        text = skill_md.read_text(encoding="utf-8")
        assert len(text) < BODY_CHAR_LIMIT, (
            f"{skill_md.relative_to(REPO_ROOT)}: {len(text)} chars > {BODY_CHAR_LIMIT}"
        )


# ---------------------------------------------------------------------------
# Link integrity / install referencability
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r"\]\(([^)#\s]+)\)")
_LOCAL_REF_RE = re.compile(r"`(references/[A-Za-z0-9_.\-]+)`")


class TestLinkIntegrity:
    """Every referenced path exists; every file is referenced (Hermes rule)."""

    def test_markdown_links_resolve(self) -> None:
        """No SKILL.md links to a missing file."""
        broken: List[str] = []
        for skill_md in CATEGORY_DIR.glob("*/SKILL.md"):
            for link in _LINK_RE.findall(skill_md.read_text(encoding="utf-8")):
                if link.startswith(("http://", "https://")):
                    continue
                target = (skill_md.parent / link).resolve()
                if not target.exists():
                    broken.append(f"{skill_md.parent.name}: {link}")
        assert not broken, f"broken skill links: {broken}"

    def test_every_file_referenced_by_name(self) -> None:
        """references//examples/ files are named in their own SKILL.md.

        Hermes installs only files referenced by name from a SKILL.md, so an
        unreferenced file is dead weight in the repo and invisible to the
        production agent.
        """
        unref: List[str] = []
        for skill_dir in _skill_dirs():
            text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            named = set(_LOCAL_REF_RE.findall(text))
            named |= {f"examples/{m}" for m in re.findall(r"examples/([A-Za-z0-9_.\-]+)", text)}
            for subdir in ("references", "examples"):
                base = skill_dir / subdir
                if not base.exists():
                    continue
                for path in sorted(base.iterdir()):
                    if f"{subdir}/{path.name}" not in named:
                        unref.append(f"{skill_dir.name}/{subdir}/{path.name}")
        assert not unref, f"files not referenced from a SKILL.md: {unref}"

    def test_subskill_read_next_names_references(self) -> None:
        """Each generated sub-skill's Read next names its reference files."""
        for theme in gen.THEMES:
            text = (CATEGORY_DIR / theme.name / "SKILL.md").read_text(encoding="utf-8")
            assert "references/semantics.md" in text, theme.name
            assert "references/snippets.yaml" in text, theme.name
            for ref in theme.extra_refs:
                assert f"`{ref}`" in text, f"{theme.name}: {ref}"


# ---------------------------------------------------------------------------
# Prose token audit (curated semantics + registry strings)
# ---------------------------------------------------------------------------


class TestProseTokens:
    """Backticked snake_case tokens in prose must name real things."""

    def _texts(self) -> Dict[str, str]:
        """Collect every prose text under audit (path/scope -> text).

        Returns:
            Mapping of source label to prose text.
        """
        texts: Dict[str, str] = {}
        for path in sorted(CATEGORY_DIR.glob("*/references/semantics.md")):
            texts[str(path.relative_to(REPO_ROOT))] = path.read_text(encoding="utf-8")
        main = CATEGORY_DIR / "generate-engraving-job" / "SKILL.md"
        texts[str(main.relative_to(REPO_ROOT))] = main.read_text(encoding="utf-8")
        for theme in gen.THEMES:
            texts[f"registry:{theme.name}"] = "\n".join(
                [theme.core, theme.title, *theme.use_when, *theme.pitfalls]
            )
        return texts

    def test_tokens_resolve(self) -> None:
        """Every backticked snake_case token is a real field/option/identifier."""
        allowed = _allowed_tokens()
        bad: List[str] = []
        for where, text in self._texts().items():
            for token in _SNAKE_RE.findall(text):
                if token in allowed or _ID_SUFFIX_RE.search(token):
                    continue
                bad.append(f"{where}: `{token}`")
        assert not bad, f"prose names unknown fields/identifiers: {bad}"


# ---------------------------------------------------------------------------
# Snippet YAML validity
# ---------------------------------------------------------------------------


class TestSnippetsYaml:
    """Generated snippets.yaml files are valid YAML with real fields."""

    def test_all_snippets_parse_and_reference_real_fields(self) -> None:
        """Each fragment parses, is a `job:` mapping, uses real field names."""
        allowed = _allowed_tokens()
        problems: List[str] = []

        def walk(node: Any, where: str) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if (
                        isinstance(key, str)
                        and _SNAKE_RE.fullmatch(f"`{key}`")
                        and key not in allowed
                    ):
                        problems.append(f"{where}: unknown field `{key}`")
                    walk(value, where)
            elif isinstance(node, list):
                for item in node:
                    walk(item, where)

        for theme in gen.THEMES:
            path = CATEGORY_DIR / theme.name / "references" / "snippets.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert isinstance(data, dict), path
            for slug, entry in data.items():
                where = f"{theme.name}/{slug}"
                try:
                    parsed = yaml.safe_load(entry["yaml"])
                except (yaml.YAMLError, KeyError, TypeError) as exc:
                    problems.append(f"{where}: bad fragment: {exc}")
                    continue
                if not isinstance(parsed, dict) or "job" not in parsed:
                    problems.append(f"{where}: no `job:` mapping")
                    continue
                walk(parsed["job"], where)
        assert not problems, f"snippet problems: {problems}"
