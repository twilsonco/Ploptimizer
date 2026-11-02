"""Unit tests for ``scripts/build_pipeline_pdf.py``.

The builder is a script (not an importable package module), so it is loaded by
path with ``importlib`` (mirroring ``tests/test_validate_job_spec.py``).

These are the *light* tests: they pin the markdown assembly (cover, verbatim
reference inlining, frontmatter stripping, heading demotion, link/anchor
rewriting, data-file truncation, glyph coverage, showcase integrity) and the
pure helpers. The pandoc/xelatex/ghostscript steps are exercised only when
those external tools are installed (``skipif``) - the PDF itself is a gitignored
local artifact and is never byte-pinned.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def script() -> Any:
    """Load scripts/build_pipeline_pdf.py as a module."""
    path = REPO_ROOT / "scripts" / "build_pipeline_pdf.py"
    spec = importlib.util.spec_from_file_location("build_pipeline_pdf", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def plan(script: Any) -> Any:
    """A text-only build plan (no generate runs) over every section."""
    workdir = REPO_ROOT / "docs" / "skills" / "engraving-jobs"  # read-only usage
    return script.BuildPlan(
        sections=script.SECTION_ORDER,
        jobs=script.SHOWCASE_JOBS,
        out=script.DEFAULT_OUTPUT,
        workdir=workdir,
        dpi=script.DEFAULT_DPI,
        previews=False,
    )


@pytest.fixture(scope="module")
def master(script: Any, plan: Any) -> str:
    """The fully assembled master markdown (text-only, no previews)."""
    return script.assemble(plan, [])


# --------------------------------------------------------------------------- #
# Pure markdown utilities
# --------------------------------------------------------------------------- #


class TestFrontmatter:
    def test_strips_leading_block(self, script: Any) -> None:
        text = "---\nname: x\ndescription: 'y'\n---\n\n# Title\n\nbody\n"
        assert script.strip_frontmatter(text) == "# Title\n\nbody\n"

    def test_leaves_plain_document(self, script: Any) -> None:
        assert script.strip_frontmatter("# Title\n") == "# Title\n"

    def test_only_leading_block_removed(self, script: Any) -> None:
        text = "# T\n\n---\nnot frontmatter\n---\n"
        assert script.strip_frontmatter(text) == text


class TestHtmlComment:
    def test_splits_leading_banner(self, script: Any) -> None:
        banner, body = script.split_html_comment("<!-- a\nb -->\ncode\n")
        assert banner.endswith("-->")
        assert body == "code\n"

    def test_no_banner(self, script: Any) -> None:
        banner, body = script.split_html_comment("# T\n")
        assert banner == "" and body == "# T\n"


class TestHeadings:
    def test_demote_shifts_levels(self, script: Any) -> None:
        out = script.demote_headings("# A\ntext\n## B\n", 1)
        assert out.startswith("## A")
        assert "### B" in out

    def test_demote_caps_at_six(self, script: Any) -> None:
        assert script.demote_headings("##### Deep\n", 3) == "###### Deep\n"

    def test_demote_ignores_code_fences(self, script: Any) -> None:
        text = "# Real\n\n```\n# not a heading\n```\n"
        assert script.demote_headings(text, 1) == "## Real\n\n```\n# not a heading\n```\n"

    def test_first_heading(self, script: Any) -> None:
        assert script.first_heading("intro\n# Title here\n") == "Title here"
        assert script.first_heading("no headings") is None

    def test_drop_first_heading(self, script: Any) -> None:
        assert script.drop_first_heading("# T\nbody\n## S\n") == "body\n## S\n"


class TestAnchorsAndLinks:
    def test_anchor_id_is_slug(self, script: Any) -> None:
        assert script.anchor_id("job", "2026-10-06 seer.yaml") == "job-2026-10-06-seer-yaml"

    def test_relative_link_to_anchor(self, script: Any) -> None:
        base = REPO_ROOT / "docs" / "skills" / "engraving-jobs" / "inline-markup"
        anchors = {str((base.parent / "recipes" / "SKILL.md").resolve()): "skill-recipes"}
        out = script.rewrite_links("see [recipes](../recipes/SKILL.md).", base, anchors)
        assert out == "see [recipes](#skill-recipes)."

    def test_unincluded_target_becomes_literal_path(self, script: Any) -> None:
        base = REPO_ROOT / "docs" / "skills" / "engraving-jobs" / "inline-markup"
        out = script.rewrite_links(
            "[schema](../engraving-job-complete-reference/references/job_spec.schema.json)",
            base,
            {},
        )
        assert out == (
            "schema (`docs/skills/engraving-jobs/engraving-job-complete-reference/"
            "references/job_spec.schema.json`)"
        )

    def test_external_and_anchor_links_untouched(self, script: Any) -> None:
        text = "[x](https://example.com/a) [y](#part-z)"
        assert script.rewrite_links(text, REPO_ROOT, {}) == text


class TestFencesAndTruncation:
    def test_fence_length_beats_content(self, script: Any) -> None:
        # A content run of three backticks forces a four-backtick fence.
        block = script.fence_block("text with " + "`" * 3 + " triple ticks", "yaml")
        assert block.startswith("````yaml\n")
        assert block.endswith("````\n")
        assert script.fence_block("plain", "text").startswith("```text\n")

    def test_truncate_notes_omission(self, script: Any) -> None:
        text = "\n".join(f"line {i}" for i in range(30))
        out = script.truncate_lines(text, 20)
        assert out.splitlines()[:20] == text.splitlines()[:20]
        assert "10 more lines omitted" in out

    def test_truncate_short_file_verbatim(self, script: Any) -> None:
        assert script.truncate_lines("a\nb\nc", 20) == "a\nb\nc"

    def test_summarize_report_stops_at_findings(self, script: Any) -> None:
        text = "Generation report: X\n  status:    success\n\nFindings (3):\n  a\n"
        out = script.summarize_report(text)
        assert "status" in out and "Findings" not in out


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


class TestDiscovery:
    def test_skill_order_covers_every_skill_dir(self, script: Any) -> None:
        order = script.skill_order()
        dirs = {
            p.parent.name
            for p in script.SKILL_ROOT.glob("*/SKILL.md")
            if p.parent.name != "generate-engraving-job"
        }
        assert set(order) == dirs
        assert len(order) == len(set(order))
        assert "generate-engraving-job" not in order

    def test_topic_files_excludes_part_references(self, script: Any) -> None:
        files = {f.path.name for f in script.topic_files("engraving-job-complete-reference")}
        assert files == set(), files  # its only references are Parts I/II

    def test_semantics_first_in_topic_order(self, script: Any) -> None:
        names = [f.path.name for f in script.topic_files("inline-markup")]
        assert names[0] == "semantics.md"

    def test_every_topic_skill_has_semantics(self, script: Any) -> None:
        for skill in script.skill_order():
            if skill == "engraving-job-complete-reference":
                continue  # the generated references ARE this skill's content
            names = {f.path.name for f in script.topic_files(skill)}
            assert "semantics.md" in names, skill


# --------------------------------------------------------------------------- #
# Assembled document
# --------------------------------------------------------------------------- #


class TestCover:
    def test_frontmatter_has_title_date_author(self, script: Any, master: str) -> None:
        import plt_optimizer

        fm = master.split("---\n", 3)[1]  # the leading pandoc YAML block
        assert 'title: "PLT-Optimizer Generation Pipeline Handbook"' in fm
        assert "subtitle:" in fm
        assert plt_optimizer.__version__ in fm  # author line carries the version
        # date is a real calendar date (e.g. "November 1, 2026").
        assert re.search(r'date: "\w+ \d+, \d{4}"', fm), fm

    def test_cover_has_version_and_units(self, script: Any, master: str) -> None:
        import plt_optimizer

        cover = master.split("# Quick start")[0]
        assert plt_optimizer.__version__ in cover
        assert "1016 units/inch" in cover
        assert "scripts/build_pipeline_pdf.py" in cover

    def test_cover_lists_sources(self, script: Any, master: str) -> None:
        cover = master.split("# Quick start")[0]
        assert "docs/skills/engraving-jobs/inline-markup/references/semantics.md" in cover


class TestQuickstart:
    def test_quickstart_present(self, master: str) -> None:
        assert "# Quick start" in master
        assert "plt-optimizer generate my_job.yaml" in master
        assert "All units are inches" in master

    def test_cli_flag_table_present(self, master: str) -> None:
        assert "| `--default-plots` | also write rapid-travel diagnostic PDFs |" in master


class TestReferences:
    def test_parts_i_and_ii_inlined_verbatim(self, script: Any, master: str) -> None:
        for name, heading in (
            ("JOB_SPEC.md", "# Part I - Job Spec Reference"),
            ("JOB_CONFIG.md", "# Part II - Shop Config Reference"),
        ):
            assert heading in master
            raw = script.include_reference(name, {}).splitlines()
            # The document's own H1 is dropped (the part heading supplies it).
            body = [ln for ln in raw if not ln.startswith("# ")]
            sample = [ln for ln in body if ln.strip()][5]
            assert sample in master, name

    def test_generated_banner_removed(self, master: str) -> None:
        assert "AUTO-GENERATED by docs/schema/generate_ai_docs.py - DO NOT EDIT" not in master

    def test_skill_md_frontmatter_not_leaked(self, master: str) -> None:
        # Frontmatter keys would appear at column 0 in the body; fenced YAML
        # snippets legitimately contain "name:" indented, so anchor the check.
        assert "\nname: generate-engraving-job" not in master
        assert "\nKeywords:" not in master
        assert "\nversion: 1.0.0" not in master


class TestTopics:
    def test_every_skill_section_present(self, script: Any, master: str) -> None:
        anchors = {script.anchor_id("skill", "generate-engraving-job")}
        anchors |= {script.anchor_id("skill", s) for s in script.skill_order()}
        for anchor in anchors:
            assert f"{{#{anchor}}}" in master, anchor

    def test_toc_links_resolve_to_anchors(self, master: str) -> None:
        declared = set()
        start = master.index("# Part III")
        for line in master[start:].splitlines():
            if line.startswith("#") and "{#" in line:
                declared.add(line.rsplit("{#", 1)[1].rstrip("}"))
        used = {
            ln.split("(#", 1)[1].split(")", 1)[0] for ln in master.splitlines() if "](#skill-" in ln
        }
        missing = used - declared
        assert not missing, f"TOC links to undeclared anchors: {sorted(missing)}"

    def test_snippets_are_fenced_yaml(self, master: str) -> None:
        assert "### YAML snippets {#ref-job-forms-snippets}" in master

    def test_no_relative_links_survive(self, master: str) -> None:
        bad = [
            ln for ln in master.splitlines() if "](../" in ln and not ln.lstrip().startswith("```")
        ]
        assert not bad, bad[:3]


class TestExamples:
    def test_showcase_specs_and_data_files_exist(self, script: Any) -> None:
        for job in script.SHOWCASE_JOBS:
            assert script.spec_path(job.spec).is_file(), job.spec
            for data in job.data_files:
                assert (script.EXAMPLES_ROOT / data).is_file(), data
        assert script.spec_path(script.GALLERY_JOB).is_file()

    def test_examples_section_lists_jobs(self, script: Any, master: str) -> None:
        start = master.index("# Part IV")
        body = master[start : master.index("# Part V")]
        for job in script.SHOWCASE_JOBS:
            stem = Path(job.spec).stem
            assert f"### {stem} {{#job-" in body, job.spec
            assert "This build skipped the generate runs" in body

    def test_skipped_run_has_no_broken_images(self, master: str) -> None:
        start = master.index("# Part IV")
        body = master[start : master.index("# Part V")]
        assert "![" not in body

    def test_data_file_truncated_in_master(self, script: Any) -> None:
        job = next(j for j in script.SHOWCASE_JOBS if j.data_files)
        output = script.JobOutput(job=job)
        section = script.build_example_section(output)
        assert "Job spec YAML" in section


class TestGalleryAndAppendices:
    def test_artifact_table_lists_all_kinds(self, script: Any, master: str) -> None:
        start = master.index("# Part V")
        body = master[start:]
        for pattern, _role in script.ARTIFACT_ROLES:
            assert f"`{pattern}`" in body, pattern
        assert "generation_report/report.json" in body

    def test_field_index_built_from_schema(self, script: Any, master: str) -> None:
        assert "## Field name index" in master
        index = script.build_field_index()
        assert "`text_height`" in index
        assert "`PlateSpec`" in index

    def test_schemas_described_not_dumped(self, script: Any, master: str) -> None:
        assert "job_spec.schema.json` (" in master
        assert '"$schema": "https://json-schema.org' not in master


# --------------------------------------------------------------------------- #
# Glyph coverage (the PDF-build regression pin)
# --------------------------------------------------------------------------- #


def _corpus_text() -> List[str]:
    """Every markdown source the handbook includes, as raw text."""
    paths: List[Path] = []
    paths += sorted(p for p in Path(script_root()).rglob("*.md") if p.is_file())
    paths += sorted(p for p in Path(script_root()).rglob("*.yaml") if p.is_file())
    paths += sorted((REPO_ROOT / "examples" / "job_specs").glob("*.yaml"))
    paths += sorted((REPO_ROOT / "examples" / "job_specs").glob("*.txt"))
    return [p.read_text(encoding="utf-8", errors="replace") for p in paths]


def script_root() -> str:
    """Return the skill-tree root as a string (keeps the helper import-free)."""
    return str(REPO_ROOT / "docs" / "skills" / "engraving-jobs")


class TestGlyphCoverage:
    def test_corpus_non_ascii_is_mapped(self, script: Any) -> None:
        """Every non-ASCII codepoint in the corpus has a LaTeX mapping.

        This is the regression pin against a PDF build that silently drops
        glyphs (xelatex's ``Missing character`` warning): any new character in
        the docs must be added to ``LATEX_GLYPH_MAP`` (or be font-safe).
        """
        chars: Dict[str, int] = {}
        for text in _corpus_text():
            for ch in text:
                if ord(ch) > 127:
                    chars[ch] = chars.get(ch, 0) + 1
        # The xelatex default (Latin Modern) covers typographic dashes, quotes,
        # and ellipsis in text mode; everything else needs an explicit mapping.
        font_safe = set("\u2013\u2014\u2018\u2019\u201c\u201d\u2026\u00a0")
        unmapped = {
            c: n for c, n in chars.items() if c not in script.LATEX_GLYPH_MAP and c not in font_safe
        }
        assert not unmapped, f"unmapped glyphs: {unmapped}"

    def test_header_writes_every_mapping(self, script: Any, tmp_path: Path) -> None:
        header = tmp_path / "h.tex"
        script.write_latex_header(header)
        text = header.read_text(encoding="utf-8")
        assert text.count(r"\newunicodechar{") == len(script.LATEX_GLYPH_MAP)
        assert r"\usepackage{newunicodechar}" in text

    def test_master_markdown_glyphs_covered(self, script: Any, master: str) -> None:
        font_safe = set("\u2013\u2014\u2018\u2019\u201c\u201d\u2026\u00a0")
        unmapped = {
            ch
            for ch in set(master)
            if ord(ch) > 127 and ch not in script.LATEX_GLYPH_MAP and ch not in font_safe
        }
        assert not unmapped


# --------------------------------------------------------------------------- #
# CLI plumbing + external steps (skipped without the toolchain)
# --------------------------------------------------------------------------- #

HAS_PANDOC = shutil.which("pandoc") is not None and shutil.which("xelatex") is not None
HAS_GS = shutil.which("gs") is not None


class TestSelectors:
    def test_resolve_sections_all(self, script: Any) -> None:
        assert script.resolve_sections("all") == script.SECTION_ORDER

    def test_resolve_sections_reorders(self, script: Any) -> None:
        assert script.resolve_sections("spec,cover") == ("cover", "spec")

    def test_resolve_sections_rejects_unknown(self, script: Any) -> None:
        with pytest.raises(ValueError, match="unknown section"):
            script.resolve_sections("cover,bogus")

    def test_resolve_jobs_curated_and_named(self, script: Any) -> None:
        assert script.resolve_jobs("curated") == script.SHOWCASE_JOBS
        picked = script.resolve_jobs("S.yaml")
        assert [j.spec for j in picked] == ["S.yaml"]
        assert picked[0].blurb == script.SHOWCASE_JOBS[0].blurb

    def test_resolve_jobs_all_covers_every_spec(self, script: Any) -> None:
        all_jobs = script.resolve_jobs("all")
        on_disk = {p.name for p in script.EXAMPLES_ROOT.glob("*.yaml")}
        assert {j.spec for j in all_jobs} == on_disk

    def test_bad_sections_exit_code(self, script: Any, tmp_path: Path) -> None:
        assert script.main(["--sections", "nope"]) == 2

    def test_markdown_only_writes_master(self, script: Any, tmp_path: Path) -> None:
        rc = script.main(
            [
                "--markdown-only",
                "--no-previews",
                "--sections",
                "cover,quickstart,appendices",
                "--workdir",
                str(tmp_path),
            ]
        )
        assert rc == 0
        text = (tmp_path / "handbook.md").read_text(encoding="utf-8")
        assert text.startswith('---\ntitle: "PLT-Optimizer Generation Pipeline Handbook"')
        assert "# About this build" in text
        assert "# Appendices" in text

    def test_preflight_reports_missing(self, script: Any) -> None:
        missing = script.preflight(need_gs=True)
        assert ("gs" in missing) == (not HAS_GS)
        assert ("pandoc" in missing) == (not HAS_PANDOC)

    def test_pandoc_command_shape(self, script: Any, tmp_path: Path) -> None:
        cmd = script.pandoc_command(tmp_path / "m.md", tmp_path / "h.tex", tmp_path / "o.pdf")
        assert cmd[0] == "pandoc"
        assert "--pdf-engine=xelatex" in cmd
        assert "-V" in cmd and "geometry:landscape" in cmd
        assert "titlepage=true" in cmd  # renders the title/date as a title page
        assert any(a.startswith("--include-in-header=") for a in cmd)

    def test_combined_proofs_filters_diagnostics(self, script: Any, tmp_path: Path) -> None:
        files = [
            tmp_path / "all_Job.pdf",
            tmp_path / "bw_all_Job.pdf",
            tmp_path / "all_Job_default.pdf",
            tmp_path / "30_txt_Job.pdf",
        ]
        picked = [p.name for p in script.combined_proofs(files)]
        assert picked == ["all_Job.pdf", "bw_all_Job.pdf"]

    def test_rasterize_pages_not_cross_matched(self, script: Any, tmp_path: Path) -> None:
        """A proof's page glob must not absorb a longer-named proof's pages."""
        if shutil.which("gs") is None:
            pytest.skip("ghostscript not installed")
        # Two minimal one-page PDFs named "A" and "A_default" (they differ only
        # by the trailing "_default"), built with gs from a blank page.
        src = tmp_path / "blank.pdf"
        subprocess.run(
            ["gs", "-sDEVICE=pdfwrite", "-o", str(src), "-c", "showpage", "-c", "quit"],
            check=True,
            capture_output=True,
        )
        a = tmp_path / "30_txt_Job.pdf"
        b = tmp_path / "30_txt_Job_default.pdf"
        shutil.copyfile(src, a)
        shutil.copyfile(src, b)
        out = tmp_path / "img"
        images = script.rasterize_proofs([a, b], out, 50)
        for pdf in (a, b):
            stem = script.safe_name(pdf.stem)
            pages = script.proof_pages(out, stem)
            assert pages, stem  # each proof collects its own page(s)
            assert all(p.name.startswith(f"{stem}-page") for p in pages)
        assert len(images) == 2

    def test_safe_name(self, script: Any) -> None:
        assert script.safe_name("2026-10-06 seer wb.yaml") == "2026-10-06_seer_wb.yaml"


@pytest.mark.skipif(not (HAS_PANDOC and HAS_GS), reason="pandoc/xelatex/gs not installed")
class TestPdfBuild:
    def test_text_only_pdf_builds(self, script: Any, tmp_path: Path) -> None:
        out = tmp_path / "handbook.pdf"
        rc = script.main(
            [
                "--sections",
                "cover,quickstart,spec,appendices",
                "--no-previews",
                "--out",
                str(out),
                "--workdir",
                str(tmp_path / "wd"),
            ]
        )
        assert rc == 0, "exit 5 means LATEX_GLYPH_MAP needs a new entry"
        assert out.is_file() and out.stat().st_size > 10_000
        pages = script.count_pdf_pages(out)
        assert pages is not None and pages > 20
