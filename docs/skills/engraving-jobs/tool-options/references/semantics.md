# Tool Options (HPGL Header Commands) — semantics

`tool_options` is a free-form mapping on the **job** tier that sets engraver
parameters (velocity, spindle, dwell, Z clearance, …) as HPGL header commands
prepended to every generated PLT file.

## How a value becomes a header

At export, the pipeline iterates the **`job-config.json` `tool_options`
metadata** (sorted, consistent order) and for each defined option:

1. value = the spec's `tool_options[option]` if present, else the config's
   default for this file's layer,
2. `null` default → the header is omitted entirely,
3. numeric values are **clamped to the configured `min`/`max`** with a
   WARNING (the job still engraves, at the clamped setting),
4. the value is formatted with the option's HPGL `command`
   (e.g. `cutting_velocity: 1.5` → `VS1.50;`).

## The dual-default model

Each option's config default carries **two values**: `text` and
`borders_holes`, because the text layer and the border/hole layer are written
to **separate PLT files** and the engraver may need different settings for
each (e.g. a faster cut on fills, slower on borders). Setting a key in the
spec applies to **both** layers of that job.

## The nine options

The live table (names, HPGL commands, types, units, ranges, and both defaults
from the shop `job-config.json`) is in `../SKILL.md` under "Available
options" and in `../../engraving-job-complete-reference/references/JOB_SPEC.md`. The set is shop-defined: read the
config, do not assume.

## Strict-mode exception — the sharp edge

`tool_options` is the one mapping the schema does NOT key-check (its keys are
engraver option names, not schema fields). **An option key the config does
not define is silently ignored** — no header is emitted, no error is raised.
A typo (`cutting_speed` for `cutting_velocity`) therefore engraves at the
shop default. Spell keys from the config's `tool_options` object and verify
the header in the output PLT when a setting is safety-critical.

## Scoping notes

- Job-level only (there is no label/line `tool_options`).
- Headers land in every PLT of the job (text and border/hole files), so they
  are per-JOB, not per-label.
- `z_clearance` is the tool-up travel height; its header value uses the
  Z-units scale (1000 units/inch) — the tool converts; you author inches.
- Values outside the configured range are clamped (WARNING), never rejected.

```yaml
job:
  job_name: "Deep engrave"
  tool_options:
    cutting_velocity: 0.5     # slow, in/sec (range from config)
    spindle_speed: 18000      # rpm
    dwell_time: 120           # ms
  content:
    - text: "DEEP CUT"
```
