# `arc_compression/` — EngraveLab horizontal-compression reference exports

Ground-truth data for **arc-preserving non-uniform glyph transforms**
(`plt_optimizer/generate/arc_fit.py`, `TextBlock.compress_x`). Copied verbatim
from `examples/plt_horizontal_text_compression/` (unit tests may not read
`examples/` — see `tests/AGENTS.md`). Source: Cadlink EngraveLab Expert v10,
single characters, HPGL at the device default scale.

## Files

`{O,C,S,8}_{no-compress,0.1-x-compress,0.5-x-compress}.plt` — twelve files,
three PLT fonts' worth of curved capitals at three horizontal-compression
settings. Each is one pen-down outline: `PU`-led, `PD;AA{cx},{cy},{sweep}`
arcs, plus the 0–3 genuinely straight strokes.

## Decoded parameters (measured, pinned by `tests/test_arc_compression_parity.py`)

- **X-scale anchor**: `x = 1373.213` plotter units — the text-box left edge,
  identical in all twelve files. Compression scales X toward this edge; Y is
  untouched (every glyph's height stays ~452 units).
- **Actual scales**: the filename number is the EngraveLab compression
  *setting*, not the factor. Measured from the x-extents against the anchor:

  | setting  | scale | O width | C width | S width | 8 width |
  |----------|-------|---------|---------|---------|---------|
  | `no`     | 1.0   | 301.7   | 285.6   | 333.2   | 251.5   |
  | `0.1`    | 0.9   | 271.5   | 257.0   | 300.3   | 226.4   |
  | `0.5`    | 0.5   | 150.9   | 142.8   | 168.0   | 125.9   |

- **Arc counts** (the behaviour we are matching):

  | glyph | s=1.0 | s=0.9 | s=0.5 |
  |-------|-------|-------|-------|
  | O     | 34    | 33    | 42    |
  | C     | 41    | 42    | 44    |
  | S     | 44    | 44    | 49    |
  | 8     | 70    | 71    | 82    |

  EngraveLab **never polygonizes**: the arc count grows by at most 1.25x.

## The two contracts these files pin

1. **The target geometry is the exact affine image.** The two-sided Hausdorff
   distance between `scale_x(uncompressed, s)` and the reference's compressed
   outline is <= 0.0124in (O 0.00005/0.00038, C 0.00081/0.00407,
   S 0.00178/0.00964, 8 0.00248/0.01236 at s=0.9/0.5). There is no hidden
   re-typesetting — compressing a glyph means scaling its geometry.
2. **Arcs stay arcs.** A circular arc maps to a parametric ellipse arc
   (`P(t) = C + cos t*U + sin t*V`, `t` the *source center* angle), which is
   what `arc_fit.py` fits back with circular `AA` arcs.

EngraveLab's own fit tolerance is loose (up to 0.012in); ours is
`ARC_FIT_DEV_TOL_INCHES` = 0.0005in, i.e. ~2x tighter at the s=0.5 worst case.
Per-arc index pairing is deliberately NOT asserted — EngraveLab re-tessellates
the whole outline, so these files are compared **as curves**, never arc-by-arc.
