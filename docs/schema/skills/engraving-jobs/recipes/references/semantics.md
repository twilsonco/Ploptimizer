# Recipes — semantics

The recipe YAMLs in `examples/` are complete, validated specs. This file is
about **choosing** one and **adapting** it safely.

## Choosing a recipe

| Job shape | Start from |
|---|---|
| One label, a few lines | `S.yaml` |
| Long text that must fit a width | `S-compressed.yaml` |
| Checklist / numbered procedure | `lists_demo.yaml` |
| Many copies from a data file | `replacement_job.yaml` |
| Circular tag | `round_labels_demo.yaml` |
| Rounded-corner placard | `rounded_rect_demo.yaml` |
| Mixed descenders (`g j p q y`) near caps | `descender_adjust_demo.yaml` |
| Real multi-label production job | `2026-10-06-seer.yaml` |

## Adapting rules

1. **Copy the file, then change `job_name` first.** It seeds output naming
   (`job_id`) — a new job should not overwrite an old run's files.
2. **Keep label `id`s unique and filesystem-safe** (they become file stems).
3. **Paths are relative to the YAML.** A recipe using
   `replacement_text_file` has its `.txt` beside the ORIGINAL spec in
   `examples/job_specs/` — copy both, or fix the path.
4. **Units are inches everywhere.** When adapting from a drawing in
   fractions, convert once, at authoring time.
5. **Do not hardcode shop defaults.** Recipes omit `hole_margin`,
   `max_h_compress`, etc. and inherit `job-config.json`; keep the omission
   when adapting — that is what lets the shop retune centrally.
6. **Validate after editing** with `parse_yaml` (see the
   running-and-troubleshooting skill) before the first run.

## Production reference: `2026-10-06-seer.yaml`

A real shop job worth studying end to end: multiple labels with per-label
`content`, per-line `text_height` overrides, `holes`, and plate sizing. Its
sibling `.txt` files in `examples/job_specs/` are the batch data. When
copying it, change `job_name` and every `id`, and re-check the plate count
against the stock on the machine.

## Extending the set

The recipe list is curated in `docs/schema/generate_skills.py` (`RECIPES`);
a new `examples/job_specs/*.yaml` becomes a recipe by adding one registry
entry — the generator copies it into `examples/` and the test suite
re-validates it through `parse_yaml` on every run.
