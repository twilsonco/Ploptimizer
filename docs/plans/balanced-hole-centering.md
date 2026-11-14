# Gap-balanced centering for a lone top/bottom drill hole

Rule: a label with a lone `top` (or lone `bottom`) drill hole centers its text
block so the gap to that hole equals the gap to the OPPOSITE label border,
swept-edge to swept-edge, clamped to the alignment content band. Opt-in
(`balanced_centering`, default off); the legacy hole-aware shift stays.

Status:
- Stage 1 solver — `plt_optimizer/generate/collision/balance.py`, committed `2d9e24b`.
- Stage 2 renderer integration — `label_renderer.py` `_balance_shift_inches`, in tree.
- Stage 3 cascade param — `schema.py` (LabelAttributes + PlateSpec parity),
  `resolution.py` (`DEFAULT_BALANCED_CENTERING`, `ResolvedLabel.balanced_centering`),
  generated docs/schemas regenerated.
- Stage 4 packing-path parity — NOT IMPLEMENTED (below).
- Stage 5 shop default — `job-config.json` currently ships `balanced_centering: true`.

## Stage 4 (packing-path parity) — found, not implemented

`_center_text_layer_vertically` is the placement for an exported label. The
packing path re-derives that same placement from structured geometry, so this
rule must be mirrored there or a packed label centers the legacy way while an
unpacked one centers balanced:

- `plt_optimizer/generate/plate_optimizer.py:217` `_label_transform`:
  `expected_center = (height/2 - band_center_offset - hole_aware - collision) *
  _UNITS_PER_INCH`, whose docstring says "Mirrors _center_text_layer_vertically
  exactly." The balance shift is a fifth term and is absent.
- It reads `rendered.text_chunks` through `_block_text_y_extents` (:146, which
  mirrors `_collect_hpgl_geometry`'s pen/arc quirks), not a file, so
  `_balance_shift_inches` cannot be called as written.

Plan: factor the solve out of the file parsing into
`collision.balance.balance_shift(label, *, bottom_ink, top_ink, x_min, x_max,
border_ink, holes, band, threshold)` on structured inputs. The renderer feeds it
pens parsed from the file; the optimizer feeds it `text_chunks` extents + the
plate's structural layer (borders/holes are the layer it already parses, :949).
One implementation of the frame/cap logic, and a parity test that asserts
packed == unpacked placement.
