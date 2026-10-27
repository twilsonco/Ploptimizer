# Running the CLI and Fixing Failures — semantics

## The command

```bash
plt-optimizer generate <spec.yaml> [-o OUT] [--job-config job-config.json]
                   [--tools tools.json] [--fast-mode] [--no-plots]
                   [--default-plots] [-v]
```

- `<spec.yaml>` — the job spec (positional, required).
- `-o/--output` — output root (default: the spec's directory); creates `plt/`
  and `pdf/` beneath it.
- `--job-config` — shop defaults JSON (default `job-config.json` in the CWD;
  missing file = silent code fallbacks + no required-field gate).
- `--tools` — cutter inventory JSON (default `tools.json` in the CWD; missing
  = ideal cutters, no compression-driven downsize).
- `--fast-mode` — single travel strategy (fast, longer rapid travel).
- `--no-plots` — PLTs only, skip PDF previews.
- `--default-plots` — also write the rapid-travel diagnostic PDFs.
- `-v/--verbose` — DEBUG logging.

Exit code 0 = success; non-zero = failure with the reason logged.

## Validating without running

```python
from plt_optimizer.generate.schema import parse_yaml

parse_yaml("my_job.yaml")   # raises ValueError with the exact path on failure
```

`parse_yaml` is the authoritative gate: schema structure + every cross-field
rule (form exclusivity, delimiter guards, placeholder rules, markup
validation, font names, hole expansion). The JSON Schemas
(`job_spec.schema.json`, `job_config.schema.json`) cover structure only —
use them in IDEs/validators for fast structural feedback, `parse_yaml`
before a production run.

For the CLI form of the same check (plus data-file existence and the
cutter-inventory shape), use the validating-jobs skill's
`scripts/validate_job_spec.py` / `scripts/validate_job_config.py` helpers.

## Error taxonomy → agent action

All spec errors raise `ValueError` subclasses with the failing path in the
message. Match the class (or the message prefix) and apply the fix:

| Error | Meaning | Agent action |
|---|---|---|
| schema validation (`ValueError` w/ path like `labels.0.content.0.tex_height`) | unknown key, wrong type, out-of-bounds value | fix the field at that exact path; check spelling against `JOB_SPEC.md` |
| `JobConfigError` | required-when-unconfigured field missing from config AND spec; or config file invalid | add the field to the spec (or the shop config): `max_h_compress`, `hole_margin`, `min_hole_margin`, `hole_text_collision_distance`, `plate_width`/`plate_height` |
| `CutterSizeError` | `cutter_size` >= the line's `text_height` | raise `text_height` or lower `cutter_size` |
| `MarkupError` | broken inline markup (unopened/unlosed tag) | balance the tags; a literal `<` pair that is not markup engraves verbatim only when it does not name a tag |
| `LineContentConfigError` | two different `optimize_line_content_max_lines` caps inside one reflow group | separate the groups with `new_line: true` or unify the cap |
| `SubstitutionError` | replacement-file line has fewer items than the template's `placeholder` lines | fix the data file or the template |
| `FontNotFoundError` | unknown font name (spec or `<f:>`) | list valid names: `python docs/schema/generate_schema_docs.py --show-fonts` |
| `LabelRenderError` | a collision (text-text / text-hole) nothing can clear | give the text room: bigger label, `max_h_compress`, `min_hole_margin`, fewer/smaller holes, shorter text |
| `LayoutFitError` | a label (or its pinned plate) cannot fit the declared plates | grow the plates, shrink labels, allow rotation, add plates |
| file-empty errors (replacement file with no data lines) | the batch file is all comments/blank | fix the data file/delimiter/comment marker |

## Outputs

- `plt/[<plate>_][<material>_]<cutter>[_<color>]_<kind>_<job_id>.plt` — one
  tool per file; `kind` = `txt` (text layer) / `bh` (borders + holes).
- `pdf/<same stem>.pdf` — proof per PLT;
  `pdf/[<plate>_][<material>_]all_<job_id>.pdf` — combined per-plate proof
  (with `--default-plots`: `..._all_<job_id>_default.pdf` rapid-travel view).
- The **layout report** (run log, and the `--verbose` output) lists per
  label: plate + position + rotation, final cutter per line (incl.
  downsizes), compressions, descender relief, hole sweeps. Read it before
  blaming the geometry.

## Determinism

Identical spec + `job-config.json` + `tools.json` + version ⇒ identical
engraving. The pipeline is seeded and ordered deterministically; re-running
an unchanged job produces byte-equal PLTs. If the shop reports a label
"changed", diff the spec/config, not the run.
