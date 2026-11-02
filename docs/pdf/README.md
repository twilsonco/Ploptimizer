# `docs/pdf/` — Compiled PDF Handbook (build artifact)

This directory holds the **generated PDF handbook** for the generation
pipeline. The PDF is a **local build artifact and is gitignored**
(`docs/pdf/*.pdf`); only this README is committed. Rebuild it with the script
below — never hand-edit the PDF, and never byte-pin it in a test.

The handbook assembles everything a shop needs in one printable, searchable,
hyperlinked document:

- **Title page** — handbook title, subtitle, `plt_optimizer` version, and the
  build date (also written into the PDF's document properties, so the file
  shows its title in viewers).
- **"About this build"** — tool version, git commit, build date, toolchain, and
  the exact list of source files included (so a printed copy is traceable).
- **Quick start** — the author → validate → run → check loop, the CLI flag
  table, and the non-negotiable invariants.
- **Part I — Job Spec Reference** — `JOB_SPEC.md` inlined verbatim (every
  `JobSpec` model field table, enums, resolved fallback defaults, the
  required-when-unconfigured gate, the live tool-options table).
- **Part II — Shop Config Reference** — `JOB_CONFIG.md` inlined verbatim.
- **Part III — Semantics by Topic** — every sub-skill's `SKILL.md` + its
  curated `references/semantics.md` (and `fallback-defaults.md` /
  `snippets.yaml`), in the main skill's TOC order.
- **Part IV — Worked Examples** — a curated showcase of example specs from
  `../../examples/job_specs/`, each with its full YAML, its batch data file
  (first 20 lines), the generation report it produced, and the **rendered plate
  proof** (the pipeline's own `pdf/all_*.pdf`, rasterized to a page image).
- **Part V — Output Artifacts** — every file a run writes (PLTs, per-cutter and
  combined proofs, rapid-travel diagnostics, the generation report), shown for
  a `--default-plots` gallery run.
- **Appendices** — JSON-Schema pointers and a field-name → declaring-models
  index.

## Prerequisites (external build-time tools)

These are **not** Python dependencies — they are system tools the script shells
out to. The script preflights them and exits with an actionable message when one
is missing.

| Tool | Used for | Install (macOS) |
|---|---|---|
| `pandoc` | markdown → PDF | `brew install pandoc` |
| `xelatex` (TeX Live / MacTeX) | the PDF engine | `brew install --cask mactex-no-gui` |
| `gs` (ghostscript) | rasterize the proof PDFs, count pages | `brew install ghostscript` |

## Build

```bash
uv run python scripts/build_pipeline_pdf.py
```

Writes `plt-generation-pipeline.pdf` (landscape US letter, ~460 pages, ~2 MB).
The example showcase runs `plt-optimizer generate` once per spec (~10-25 s
each), so a full build takes a few minutes.

Useful flags:

| Flag | Effect |
|---|---|
| `--markdown-only` | write the assembled master markdown and stop (no PDF toolchain needed) |
| `--no-previews` | skip the generate runs - fast, text-only PDF (examples show YAML only) |
| `--sections <list>` | subset of `cover,quickstart,spec,config,topics,examples,gallery,appendices` |
| `--jobs curated\|all\|<a.yaml,b.yaml>` | which example specs to feature |
| `--dpi <n>` | preview rasterization density (default 200) |
| `--keep-workdir` + `--skip-generate` | reuse a previous run's output for fast typography iteration |
| `-o <path>` | output PDF path |

## Regenerate after doc changes

The handbook is a **renderer of committed artifacts** - it reads
`../skills/engraving-jobs/**` as-is and never re-derives schema content, so it
cannot drift from the Pydantic models. After changing a `Field(description=...)`,
a generator string, a curated `semantics.md`, or an example spec, regenerate the
artifacts (per [`../schema/README.md`](../schema/README.md)) and then rebuild the
PDF here.

The curated showcase list lives in `SHOWCASE_JOBS` in
[`../../scripts/build_pipeline_pdf.py`](../../scripts/build_pipeline_pdf.py);
`tests/test_build_pipeline_pdf.py` fails when a listed spec or data file is
renamed or removed, so update that table alongside any example rename.
