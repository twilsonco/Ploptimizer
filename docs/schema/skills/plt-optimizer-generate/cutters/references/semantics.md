# Cutter Selection — semantics

Every engraved stroke has a cutter diameter. It comes from one of three
places — the ladder, an explicit `cutter_size`, or a markup force — and it
determines **which PLT file the stroke lands in**.

## The automatic ladder (default)

With `cutter_size: null` (the default), the cutter is auto-selected from the
line's `text_height`:

1. the ideal map (`IDEAL_CUTTER_MAP` in the resolution layer) picks a nominal
   diameter for the height,
2. the request snaps to the `tools.json` `available_cutters` inventory —
   **next size down, else next up** — with a WARNING when it moves,
3. multi-stroke (bold) fonts re-derive the diameter from the boldness ladder
   (see the bold-multistroke skill),
4. compression may step it DOWN one rung (see the
   compression-and-cutter-downsize skill).

The ladder is deterministic: the same height + inventory always yields the
same tool.

## Explicit `cutter_size`

An explicit diameter (inches, `> 0`, cascade line → label → job, **no config
tier**):

- snaps to the inventory exactly like the ladder (down, else up; WARNING when
  it moves),
- makes the toolpath height `text_height - cutter_size` (the engraved cap
  height stays at the nominal `text_height`),
- **bypasses** the ladder: boldness targets and compression-driven downsize
  are inert on that line,
- aborts the job with `CutterSizeError` when the cutter is at or above the
  line's `text_height`.

## Per-cutter PLT files

Each distinct cutter in the job — ladder, explicit, `<c:>`, `<scale:>`, `<b>`
— becomes **its own PLT file** in `plt/` (the pen map is keyed by diameter).
On the engraver, load one file per tool in the order the preview PDFs show.
A job mixing a 0.060in body and a 0.030in serial produces two files.

## `<c:D>` and `<oc>` (inline forces)

`<c:0.030>fine</c>` forces a diameter for the run (snapped like
`cutter_size`); `<oc>` returns to the line's ladder cutter. The forced run
lands in its own per-cutter file.

## Boundary/hole cutter

Borders and drill holes engrave with the **boundary/hole tool**
(`tools.json` `boundary_hole_cutter_size`, fallback
`DEFAULT_BOUNDARY_HOLE_CUTTER`). It feeds the collision stroke floor and the
hole diameter compensation (see the holes-and-collision skill) — it is not a
spec field.

## Reading the result

The layout report (run log) lists the final cutter per line, including
downsizes (`cutter 0.060in -> 0.045in`). If a job produces more PLT files
than expected, some line picked up a different cutter — check the report for
`<scale:`/`<b>`/downsize entries.
