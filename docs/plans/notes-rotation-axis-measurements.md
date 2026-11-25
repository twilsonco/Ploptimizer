# Measured: which lever actually changes rotations (probe_axis_ablation.py)

Method: for each spec, pack the real rect set with
`_pack_best(rects, bins, allow_rotation=True, layout_mode, rotation_preference)`
and report per-plate bbox fill % + rotated label count. 4 specs x 2 layout modes
x 2 rotation preferences. Raw output: `.probe/axis_matrix.txt`.

## Full matrix — measured (single `_pack_best` run per cell, 24x16 plate)

| spec | rects | layout | rotation_preference | per-plate bbox fill % | rotated |
|---|---|---|---|---|---|
| CES vons 1897 | 146 | columns | fill_axis | [100.0, 62.5] | 0 |
| CES vons 1897 | 146 | columns | minimize | [100.0, 62.5] | 0 |
| CES vons 1897 | 146 | rows | fill_axis | [100.0, 62.5] | 0 |
| CES vons 1897 | 146 | rows | minimize | [100.0, 62.5] | 0 |
| ADK CPR 111261 | 145 | columns | fill_axis | [100.0, 83.3] | **131** |
| ADK CPR 111261 | 145 | columns | minimize | [100.0, 83.3] | **131** |
| ADK CPR 111261 | 145 | rows | fill_axis | [100.0, **87.5**] | **0** |
| ADK CPR 111261 | 145 | rows | minimize | [100.0, **87.5**] | **0** |
| seer 1811 | 152 | columns | fill_axis | [100.0, 95.8] | **144** |
| seer 1811 | 152 | columns | minimize | [100.0, 95.8] | **144** |
| seer 1811 | 152 | rows | fill_axis | [100.0, 95.8] | **94** |
| seer 1811 | 152 | rows | minimize | [100.0, 95.8] | **94** |
| CES SFW3525 | 122 | columns | fill_axis | [100.0, 54.2] | **118** |
| CES SFW3525 | 122 | columns | minimize | [100.0, 54.2] | **118** |
| CES SFW3525 | 122 | rows | fill_axis | [100.0, **56.2**] | **0** |
| CES SFW3525 | 122 | rows | minimize | [100.0, **56.2**] | **0** |

`rotation_preference` is byte-identical to `fill_axis` in **all 8** comparable
cells; `layout` moves rotations in 3 of 4 specs and never makes fill worse.

Conclusions (measured, this spec):

1. **`rotation_preference` is inert on this spec** — `fill_axis` and `minimize`
   produce byte-identical plans. Within one layout mode the rectpack runs choose
   rotation for fill, and the fill-optimal rotation set is the same one both
   policies rank first, so the rank swap never flips a winner.
2. **`layout` (the rotation AXIS) is the real lever** — `columns` -> `rows`
   takes rotations 131 -> 0 **and** improves plate 2 from 83.3% to 87.5%: a
   strict win on both of the user's axes at once.
3. `job-config.json` ships `layout: rows`, which is why the production baseline
   of this spec reports 0 rotations; the 131-rotation layout only appears when a
   spec declares `layout: columns` (per-spec `layout` overrides the shop default).

## Implications for the plan

- U1 (`rotation_preference`) is honest and cheap but measured near-no-op on real
  specs; it must not be sold as the fix for "fewer rotations". Keep it as a
  low-cost preference that can flip ties, and state the measured result.
- The "fewer rotations" ask is delivered by **exposing the axis choice**
  (`layout: rows` vs `columns`) as a searched dimension, not only a declared one
  — i.e. the plan key should compare both axes (they are different layouts, and
  today `layout` is a hand-declared mode that gates which axis is searched).
- The "unutilized plate material" ask is NOT addressed by either: it is the
  material-partition / tail-sharing problem (U2, D1-b).
