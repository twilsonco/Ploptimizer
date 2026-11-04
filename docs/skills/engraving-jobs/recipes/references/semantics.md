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
| Real multi-label production job (per-label data files) | `2026-10-06-seer.yaml` |
| Multi-size production run tagged with a `material` | `2026-10-30-CES-vons-1735.yaml` |
| Engraver header settings (`tool_options`: speed, spindle) | `2026-10-06-seer-wb-uv.yaml` |
| Small round tags: explicit `cutter_size`, holes, SC font | `2026-10-15-coolsys-round-tags-target-910.yaml` |
| Uniform `count` copies on a simple grid (`layout`) | `2026-07-22-sunwest.yaml` |
| One big placard rich in inline markup | `2026-10-07-coolsys-1-big-label-sc.yaml` |

## Adapting rules

1. **Copy the file, then change `job_name` first.** It seeds output naming
   (`job_id`) — a new job should not overwrite an old run's files.
2. **Keep label `id`s unique and filesystem-safe** (they become file stems).
3. **Paths are relative to the YAML.** Each recipe's
   `replacement_text_file` data files ship beside it in `examples/` —
   copy the YAML **and** every `.txt` it references, or fix the paths.
4. **Units are inches everywhere.** When adapting from a drawing in
   fractions, convert once, at authoring time.
5. **Do not hardcode shop defaults.** Recipes omit `hole_margin`,
   `max_h_compress`, etc. and inherit `job-config.json`; keep the omission
   when adapting — that is what lets the shop retune centrally.
6. **Validate after editing** with `parse_yaml` (see the
   running-and-troubleshooting skill) before the first run.

## Production references

- `2026-10-06-seer.yaml` — multiple labels with per-label `content`,
  per-line `text_height` overrides, `holes`, and label-level replacement
  data files. When copying it, change `job_name` and every `id`.
- `2026-10-30-CES-vons-1735.yaml` — a `material`-tagged run (the tag lands
  in every output file name); its commented `plates:` blocks show how to
  pin the same job to declared plate sizes when stock is fixed.
- `2026-10-15-coolsys-round-tags-target-910.yaml` — the small round-tag
  pattern: explicit `cutter_size`, top mounting hole, small-caps font,
  job-level replacement batch.

## Extending the set

The recipe list is curated in `docs/schema/generate_skills.py` (`RECIPES`);
a new `examples/job_specs/*.yaml` becomes a recipe by adding one registry
entry — the generator copies the YAML **and every `replacement_text_file`
it references** into `examples/`, names them in `SKILL.md`, and the test
suite re-validates everything through `parse_yaml` on every run. Keep the
per-recipe `notes` short: the SKILL.md body must stay under the Hermes
soft cap, so recipe YAMLs are shipped as files, never embedded.
