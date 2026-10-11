# `rectpack` Knob Investigation — Packing Control Surface

Investigation report for the TODO item *"Investigate what other knobs/parameters
exist for `rectpack` that could be exposed to the user for better control over
the packing algorithm"* (packing labels onto plates during label generation).

**Scope & method.** Every claim below was read from the vendored
`rectpack` **0.2.2** source (`.venv/lib/python3.11/site-packages/rectpack/`,
= github.com/secnot/rectpack) and from our layout engine
(`plt_optimizer/generate/layout.py`: `PACK_CONFIGS`, `PACK_CONFIGS_COLUMNS`,
`_pack_best`). This is a **report only** — no schema, config, or engine code
was changed. Section 6 records the agreed design guidance (job-level-only
cascade) should any knob be exposed later.

---

## 1. How we pack today

`_pack_best()` runs a **best-of ensemble**: every candidate
`(pack_algo, sort_algo)` config is run over the same rectangles and bins
(× rotation off/on when `allow_rotation`), and the winner is ranked by
`(full fit, smallest plate footprint, fewest rotations)`.

| Mode | Configs | Frame |
|---|---|---|
| `layout: rows` | `MaxRectsBssf/SORT_AREA`, `GuillotineBlsfLas/SORT_NONE`, `GuillotineBlsfLas/SORT_AREA`, `GuillotineBafLas/SORT_NONE` | real plate frame |
| `layout: columns` | `SkylineBl/SORT_NONE`, `SkylineBl/SORT_AREA` | transposed frame, placements mapped back |

All runs use `mode=Offline`, `bin_algo=BFF`, and the `rotation` knob is
already user-facing as `allow_rotation` (ranked *below* footprint, so labels
rotate only when rotation strictly improves the used area).

**Consequence for knob design:** the ensemble *is* the automatic quality
mode. Any exposed knob is a **pin/override** control — it trades guaranteed
footprint quality for deterministic, user-shaped fill patterns. The default
must stay "auto = ensemble".

## 2. Full knob inventory

`rectpack.newPacker(mode, bin_algo, pack_algo, sort_algo, rotation)` plus
per-bin kwargs forwarded from `add_bin(w, h, count, **kwargs)` into the
placement-algorithm constructor.

| Knob | Full value set (0.2.2) | Our current use | User-facing effect |
|---|---|---|---|
| `pack_algo` | 28 classes (§3.1) | 2 per mode | **Pin the placement algorithm** → full control of the fill pattern |
| `sort_algo` | `SORT_AREA / PERI / DIFF / SSIDE / LSIDE / RATIO / NONE` (§3.2) | `NONE` + `AREA` | **Pin the placement order** of labels → grouping/symmetry control |
| `bin_algo` | `BNF / BFF / BBF / Global` (§3.3) | `BFF` fixed | **Multi-plate fill strategy** (which sheet gets which label) |
| `rotation` | bool | `allow_rotation` ✅ | already exposed |
| `mode` | `Online / Offline` | `Offline` | Online disables sorting entirely — no value (§3.5) |
| Guillotine `merge` | bool (bin kwarg) | default `True` | free-rectangle merging; marginal (§3.6) |
| Skyline `*Wm` | 3 classes | unused | waste-minimizing placements; niche (§3.7) |
| `add_bin(count=n)` | int | bins added one at a time | n identical sheets per call; internal detail, not a knob |

## 3. Per-knob analysis

### 3.1 `pack_algo` — the placement algorithm (28 classes)

Three algorithm families:

- **Guillotine** (18 = 3 fitness × 6 split rules). Fitness picks the free
  section: `Bssf` (best short side fit), `Blsf` (best long side fit),
  `Baf` (best area fit). Split rule picks how the leftover L-region is cut:
  `Sas` (short axis), `Las` (long axis), `Slas`/`Llas` (short/long
  leftover axis), `Maxas`/`Minas` (max/min area). Exported as
  `Guillotine{Bssf,Blsf,Baf}{Sas,Las,Slas,Llas,Maxas,Minas}`.
- **MaxRects** (4): `MaxRectsBl` (bottom-left), `MaxRectsBssf`,
  `MaxRectsBaf`, `MaxRectsBlsf`.
- **Skyline** (6): `SkylineMwf`, `SkylineMwfl`, `SkylineBl`, and the
  waste-managed `SkylineBlWm`, `SkylineMwfWm`, `SkylineMwflWm`.

Verified behavioural notes (empirical, from our own experiments):
Guillotine `Blsf/Baf` + `Las` with a wide bin fill **row-major** (X-first
along the bottom); `MaxRectsBssf` fills dimension-dependently (2nd rect goes
UP, then along the y=h strip); `SkylineBl`'s bottom-row fitness fills the
packer X axis first, which is why it powers the transposed `columns` frame.

**Exposure value: highest.** A `layout_algo: auto | <pinned class>` knob
gives users deterministic fill patterns (e.g. always clean rows, always
skyline columns) and full reproducibility per algorithm. `auto` keeps the
ensemble. All 28 names are static, so a string enum validates cleanly.

### 3.2 `sort_algo` — placement order (7 built-ins + callables)

`SORT_AREA` (descending area), `SORT_PERI` (perimeter), `SORT_DIFF`
(|w−h|), `SORT_SSIDE` (short side), `SORT_LSIDE` (long side), `SORT_RATIO`
(w/h), `SORT_NONE` (insertion = content order). All are stable descending
sorts; `NONE` preserves our content order, which is what makes
`columns`-mode reader-order re-assignment meaningful.

`sort_algo` accepts **any callable** `rectlist → rectlist` — the library's
real extension point (e.g. a sorter keeping same-`id` label instances
adjacent, or biasing tall labels to one edge). A custom callable is not
YAML-exposable, so a `layout_sort: none | area | perimeter | short_side |
long_side | ratio | diff` enum covers the user-facing part.

**Exposure value: medium.** Placement order visibly changes grouping and
symmetry (e.g. `SORT_NONE` keeps the authored label order along the fill
axis; `SORT_AREA` packs big-first and scatters identical labels).

### 3.3 `bin_algo` — bin (plate) selection (4 mixins)

Orthogonal to the placement algorithm; decides which *sheet* a rectangle
goes to:

- `BNF` (Next Fit): **one open plate at a time** — plate 1 fills completely
  before plate 2 opens. The natural scrap workflow ("exhaust sheet 1
  before touching sheet 2").
- `BFF` (First Fit): first open plate the rect fits — our pin; spreads
  across open plates in open order.
- `BBF` (Best Fit): the open plate with the best *fitness* for the rect.
- `Global`: fills each plate greedily with the globally fittest remaining
  rect, then closes it. Silently forces `sort_algo=None`.

**Exposure value: medium.** `bin_algo: bff | bnf | bbf` (skip `global` —
it disables sorting and overlaps with `sort` semantics) controls
multi-sheet fill, which matters exactly for our scrap-material use case.
Note `BNF` can *increase* the footprint (it commits to a plate early);
under the ensemble it must be a pin, not an extra candidate.

### 3.4 `rotation` — already exposed

`allow_rotation` (job-level, default `True`) with the
`(footprint, rotated_count)` ranking: rotation only when it strictly
improves the footprint, all-horizontal wins ties. No change needed.

### 3.5 `mode: Online` — rejected

Online packs each rectangle at `add_rect` time and `newPacker` **nulls
`sort_algo`** in Online mode; bin selection is the only choice left. It
exists for streaming feeds we don't have. No value.

### 3.6 Guillotine `merge` bin kwarg — marginal

`add_bin(..., merge=False)` disables free-section merging in Guillotine
(constructors: `Guillotine.__init__(width, height, rot=True, merge=True)`;
kwargs flow through `BinFactory` → algorithm ctor). Merging generally helps
Guillotine recover large free areas; disabling it is a research knob, not a
shop knob. MaxRects/Skyline ignore the kwarg (Skyline hard-passes
`merge=False` to its super). **Not recommended for exposure.**

### 3.7 Skyline waste-management variants — niche

`SkylineBlWm` / `SkylineMwfWm` / `SkylineMwflWm` consult a `WasteManager`
so placements sink into detected waste slots. They are plain
`pack_algo` choices, so a `layout_algo` enum (§3.1) covers them for free —
no separate knob.

## 4. NOT available in `rectpack` (do not promise these)

- **Inter-label spacing / gutters.** `rectpack` has no margin/spacing
  parameter; rectangles pack coincident. Our labels get their spacing from
  per-label `margin` at render time — structurally the right place. A
  "gap between labels" feature would be a *pre-pass inflation* of packing
  dimensions (each rect grown by the gap), not a rectpack knob.
- **Per-bin fill-order weights / bin preferences** (e.g. "prefer the big
  sheet first"). Bin iteration order is `add_bin` order (BFF never closes
  bins); we already control that by plate declaration order.
- **Extra sorters** (`SORT_BSF`, filler-based, etc.) — do not exist in
  0.2.2; the callable extension point (§3.2) is the only way in.
- **Area-penalty / aspect objectives.** The only quality metric in the
  library is placement fitness; our `_plate_footprint` ranking is
  implemented entirely on our side.

## 5. Recommendation summary

1. **Keep the ensemble as the default** (`auto`) — it is our quality floor.
2. If one knob is ever exposed, make it **`layout_algo`** (auto + pinned
   algorithm names): best value/plumbing ratio, covers fill-pattern control
   and the Skyline Wm variants.
3. Runner-up: **`bin_algo: bff | bnf | bbf`** — `bnf` gives the
   strictly-sequential scrap-sheet fill that plate declaration order alone
   cannot guarantee.
4. `layout_sort` enum is a cheap third option; `merge`/Online are not worth
   exposing.

## 6. If implemented: threading (agreed design, not yet built)

Follow the `layout` / `allow_rotation` precedent — a **job-level-only**
global packing concern (user-approved; it never enters `ResolvedLabel`):

1. `schema.py`: `LayoutAlgo` / `LayoutSort` / `LayoutBin` str-enums +
   `JobSpec` fields (default `auto`), validated names mirroring the
   rectpack class/callable names.
2. `job_config.py`: keys in `JobDefaults` + `_JOB_LAYER_FIELDS` so
   `job-config.json` supplies shop defaults; YAML always wins.
3. Thread as keywords exactly like `layout`: CLI /
   `scripts/run_integration_test.py` → `vectorize.export_per_cutter_plts`
   → `layout.generate_layout*` → `_pack_group` → `_pack_best`, where a
   pinned value replaces the `configs` list (single candidate) instead of
   the ensemble.
4. Docs: regenerate `docs/schema/JOB_SPEC.md` + schema JSONs; document in
   AGENTS.md §6 next to `layout`.
