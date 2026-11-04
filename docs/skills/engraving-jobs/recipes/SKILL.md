---
name: recipes
description: 'Complete runnable job specs to copy and adapt. Use when: starting a job from a known-good shape; reading a full spec end to end. Keywords: example, recipe, template, yaml, copy.'
version: 1.0.0
metadata:
  hermes:
    category: engraving-jobs
    tags: [plt, engraving, toolpath, generate]
---

# Recipes — Complete Example Job Specs

## When to use

- Starting a new job: pick the closest shape below, open its file, copy and adapt.
- Learning how the fields combine in a real, validated spec.

Every recipe is copied verbatim from `examples/job_specs/` in the tool repository and re-validated through `parse_yaml` on every regeneration. The files live in `examples/` beside this skill, and each recipe's replacement data files (where it uses any) ship beside it in the same directory — a recipe runs with only its `examples/` directory. Paths inside a spec resolve relative to the YAML file, so keep data files next to the copy you run.

## Multi-line label, auto-sized

A plain multi-line label — the baseline shape every spec grows from.

Files: `examples/S.yaml`

## Compressed long text

A `max_h_compress` budget squeezing a long line into the label width.

Files: `examples/S-compressed.yaml`

## Bullet and enumerated lists

`itemize` / `enumerate` blocks with counters and hanging indent.

Files: `examples/lists_demo.yaml`

## Batch from a replacement file

A job-level `replacement_text_file` batch (its data file ships beside the spec); paths resolve relative to the YAML.

Files: `examples/replacement_job.yaml`, `examples/replacement_text_assets.txt`, `examples/replacement_text_crew.txt`, `examples/replacement_text_sample.txt`

## Round labels

Circular label shapes with content fitted inside.

Files: `examples/round_labels_demo.yaml`

## Rounded-rectangle labels

`rounded_rectangle_fraction` corner rounding on rect labels.

Files: `examples/rounded_rect_demo.yaml`

## Descender relief showcase

Lines with g/j/p/q/y beside caps — the per-glyph relief in action.

Files: `examples/descender_adjust_demo.yaml`

## Production multi-label job

A real shop job: multiple labels, per-label content and sizing, each label batched from its own replacement data file (shipped beside it).

Files: `examples/2026-10-06-seer.yaml`, `examples/2026-10-06 seer 1x3.txt`, `examples/2026-10-06 seer 7x5.33.txt`

- Label-level `replacement_text_file` (one data file per label) with `placeholder: true` template lines.
- Per-label `width` / `height` / `text_height` overriding the job tier.

## Production job with material

A real multi-size shop job: a job-level `material` tag on every output stem, three plate sizes fed by per-label replacement files.

Files: `examples/2026-10-30-CES-vons-1735.yaml`, `examples/2026-10-30 CES vons 1735 1x3.txt`

- `material: bw` — the material tag lands in every PLT/PDF file name (`[<plate>_][<material>_]<cutter>_...`), so mixed runs sort at the machine.
- Commented-out `plates:` blocks show how the same job is pinned to declared plate sizes when stock is fixed.

## Engraver tool options

A production job setting `tool_options` (cutting velocity, spindle rpm) for the HPGL header commands.

Files: `examples/2026-10-06-seer-wb-uv.yaml`, `examples/2026-10-06 seer 7x5.33.txt`

- `tool_options` keys must exist in the shop `job-config.json` metadata — an unknown key is silently ignored.
- Options apply per output layer (text vs borders/holes defaults); the spec value overrides both.

## Round tags with explicit cutter

Small round tags: explicit `cutter_size`, a top mounting hole, `dino sc` small-caps font, job-level replacement batch.

Files: `examples/2026-10-15-coolsys-round-tags-target-910.yaml`, `examples/2026-10-15 coolsys round tags target 910 b.txt`

- `cutter_size: 0.02` pins the tool (no auto-ladder selection); the toolpath height subtracts cutter + buffer.
- `font: dino sc` + `sc_scale` — a PLT small-caps family from the catalog.
- Round `type` with `holes` at `location: top` — the collision phase keeps text clear of the hole.

## Simple grid layout

A uniform-count job using `layout: columns-simple` (rectpack-free grid fill) with `count` copies.

Files: `examples/2026-07-22-sunwest.yaml`, `examples/2026-07-22 sunwest.txt`

- `layout` selects the packing engine; `*-simple` modes fill a fixed grid and never rotate.
- `count` multiplies the single root `content` label.

## Inline markup showcase

One big placard driven by inline markup: fonts, italics, scaling and per-line styling inside the text strings.

Files: `examples/2026-10-07-coolsys-1-big-label-sc.yaml`

- Markup tags (`<f:>`, `<i>`, `<scale:>`, ...) cascade per line; the inline-markup skill documents every tag.
- Shows content-entry `text` carrying rich markup, not plain strings.

## Read next

- `references/semantics.md` — choosing and adapting a recipe.
- [`../generate-engraving-job/SKILL.md`](../generate-engraving-job/SKILL.md) — the main workflow skill.
- [`../engraving-job-complete-reference/references/JOB_SPEC.md`](../engraving-job-complete-reference/references/JOB_SPEC.md) — exhaustive field reference.
- [`../engraving-job-complete-reference/references/JOB_CONFIG.md`](../engraving-job-complete-reference/references/JOB_CONFIG.md) — exhaustive shop-config field reference.
