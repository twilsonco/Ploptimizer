# AGENTS.md - System Instructions for AI Coding Assistants

## Role & Core Philosophy
You are an expert Principal Software Engineer acting as an autonomous agent in this repository. Your primary goal is to build `PLT-Optimizer`, a deterministic, cross-platform Python tool for optimizing geometric toolpaths. 

Prioritize reliability, mathematical precision, and strictly typed code over speed of delivery. Do not guess or hallucinate logic—if an implementation detail regarding HPGL/PLT parsing or Traveling Salesperson algorithms is ambiguous, stop and ask the user for clarification.

## 1. Coding Style & Standards
We adhere strictly to the **Ruff / Black** formatting standards and modern Python paradigms.
* **Strict Typing:** Every function, class, and method must have complete PEP 484 type hints. Run type checks via `mypy` before finalizing code.
* **Pre-commit hook:** Ensure that all code passes the configured pre-commit hooks before committing. This includes linting, formatting, and type checks.
* **Docstrings:** Use Google-style docstrings for all modules, classes, and public functions.
* **Immutability & Data Structures:** Prefer `dataclasses` (with `frozen=True` where appropriate) or `pydantic` models for internal state representation. 
* **Mathematical Precision:** Never use `==` for floating-point coordinate comparisons. Always use `math.isclose()` or `numpy.isclose()` with explicit tolerances.

## 2. Testing & Coverage
Testing is not an afterthought; it is a primary deliverable. 
* **Test-Driven Operations:** Every time a new function or logical block is written and confirmed working, you must write the corresponding unit test immediately.
* **Full Coverage Requirement:** Maintain 100% test coverage for all core parsing, writing, and optimization logic. Use `pytest` and `pytest-cov`.
* **Identity Testing:** Any changes to the `parser.py` or `writer.py` must pass the identity validation suite (ensuring `input.plt -> parse -> write -> output.plt` results in semantic equivalence).
* **Execution:** Run the test suite autonomously after modifying the codebase. Do not commit failing code.

## 3. Git Workflow & Commits
* **Conventional Commits:** All commit messages must strictly follow the Conventional Commits specification (e.g., `feat:`, `fix:`, `refactor:`, `test:`, `chore:`).
* **Commit Frequency:** Commit frequently to establish a granular history.
* **Working State Only:** You must only commit code that has passed all static type checks and unit tests. Never commit code with syntax errors or broken tests. 
* **NEVER Rewrite History:** Do not run `git filter-repo`, `git filter-branch`, `git rebase` on pushed branches, `git commit --amend` on pushed commits, or any other command that rewrites commit hashes. Rewriting makes local and remote histories unrelated (zero merge-base), breaks every downstream clone/PR, and invalidates GPG signatures. History rewrites require explicit human approval.
* **NEVER Touch Remotes:** Do not run `git remote remove/set-url` or delete remote-tracking refs. Note that `git filter-repo` removes all remotes by design — a second reason it is banned. Restoring a lost remote or force-pushing over a branch is a human decision, not an agent default.
* **No Destructive Operations Without Approval:** `git push --force`/`--force-with-lease`, `git reset --hard` on shared branches, branch/tag deletion, and garbage collection (`git gc --prune=now`) must never be executed autonomously.
* **Secrets Incident Protocol:** If a secret was accidentally committed, do NOT attempt to scrub history. Report the situation to the user and recommend rotating the credential; removal of committed secrets is a coordinated human decision (it requires a force-push that rewrites all history).

## 4. Project-Specific Invariants
* **Package Management:** Use **`uv`** exclusively. Do not use standard `pip`, `poetry`, or `conda`. Update `pyproject.toml` directly for dependency management.
* **Cross-Platform Compatibility:** The tool is developed on Linux but deployed on Windows. You must use `pathlib.Path` for all file system operations. Never use hardcoded strings with forward or backward slashes. Account for Windows `\r\n` line endings in file I/O where it impacts parsing.
* **Dual Logging Topology:** Any new operational logic must hook into the established logging structure:
  1. Standard text logging (`logging` module) utilizing `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.
  2. CSV Metrics logging for tracking optimization deltas (distance before/after).
* **Silent Execution:** Unless logging an error or running in verbose mode, the standard path optimization loop should execute cleanly without cluttering standard output, as it will run as a headless hot-watch service.

## 5. Python 3.8 (Windows 7) compatibility requirement for watch directory function
* **Watch Directory Function:** Ensure that the directory watching mechanism works correctly on Python 3.8 running on Windows 7. Avoid using features introduced in later Python versions. Test the function thoroughly on the target environment to confirm compatibility.
* **Plotting and development:** Plotting and benchmarking is not necessary to run on Windows 7, so this constraint only applies to the watch directory function and core operational logic. Development tools that require newer Python versions can be used for plotting and benchmarking on other environments.

## 6. YAML Job Specification & Label Generation Schema

### Overview
The `plt_optimizer/generate/schema.py` module defines the complete data contract for label generation jobs via YAML specifications. All models use **Pydantic** for validation and inherit from typed base classes supporting cascading attributes.

### Core Data Model

**Inheritance Hierarchy (Top-Down Cascade):**
```
JobSpec (job-level defaults)
  ├── LabelSpec (per-label overrides)
  │   └── TextLine (individual text rendering)
  └── PlateSpec (material sheet definitions)
```

**TextAttributes** (cascades to TextLine):
- `text_height`: Font height in inches
- `character_spacing`: Extra spacing between characters
- `line_spacing`: Extra spacing between text lines
- `max_h_compress`: Maximum horizontal compression fraction in [0, 1] (default
  0.0 = disabled). When a rendered line is wider than the label's inner
  content area, it is uniformly compressed horizontally down to at most
  `(1 - max_h_compress)` of its natural width.
- `text_h_alignment`: Horizontal alignment of a rendered text line within the
  label's inner content area. Enum `TextHAlignment` with values `left`,
  `center`, `right` (default `center`). `left` places the line's left-most
  point precisely at the left margin; `right` places the right-most point
  precisely at the right margin; `center` centers the line. Accepted on
  plates for schema parity only (not applied at plate level).
- `min_hole_margin`: Minimum hole margin in inches (`ge=0.0`, default `None`
  = collision avoidance disabled). When a rendered text line collides with a
  drill hole, `hole_margin` may be reduced toward this floor to clear it.
  Cascades label → job (accepted on plates for schema parity only).
- `hole_text_collision_distance`: Minimum air gap in inches (`ge=0.0`,
  default `None` → resolved to **0.15**) kept between the *engraved* text
  stroke and the *engraved* drill-hole stroke. Cascades label → job
  (accepted on text lines and plates for schema parity only); an explicit
  `0.0` is honored (strokes may touch but never overlap).

**LabelAttributes** (extends TextAttributes, cascades to LabelSpec only):
- `width`, `height`, `margin`: Label dimensions & safety margins
- `hole_margin`: Distance from hole edge to label edge (cascades: label → plate → job)
- `holes`: List of `HoleSpec` objects (diameter + location enum). Group
  locations `corners` (all four corners) and `sides` (left + right) are
  expanded in place into their atomic member holes at validation time
  (`HOLE_LOCATION_GROUPS`); downstream consumers only see the 8 atomic
  locations.

### Key Classes

| Class | Purpose | Validation Rules |
|-------|---------|------------------|
| `JobSpec` | Root job container | Requires either `labels` list OR root-level `content` (mutually exclusive) |
| `LabelSpec` | Individual label definition | `count >= 1`; requires `content` (min 1 TextLine) OR `replacement_text_file` (mutually exclusive with `count`) |
| `TextLine` | Text content unit | Requires non-empty `text` string |
| `PlateSpec` | Physical sheet definition | All dimensions `>= 0`, includes `clearance_padding` |
| `HoleSpec` | Drilled hole definition | Location (required; 8 atomic enum values: corners + edges, plus `corners`/`sides` group shorthands expanded at validation) + optional `diameter` (default 0.125", must be > 0) |
| `parse_yaml()` | Entry point | Returns validated `JobSpec` or raises `ValueError` |
| `expand_job_spec()` | Replacement expansion (substitution.py) | Called after `parse_yaml()`; flattens replacement-driven labels into static LabelSpecs |

### Horizontal Text Compression

Over-wide text lines are uniformly compressed horizontally (glyphs + spacing
scale together; Y is untouched) so they respect the label margins. This is
**opt-in** via `max_h_compress` in [0, 1] (default 0.0 = disabled), cascading
line → label → job (accepted on plates for schema parity only).

- `compute_horizontal_scale()` (resolution.py): pure scale-factor math,
  clamped to `[1 - max_h_compress, 1.0]`.
- `compress_line_to_width()` (label_renderer.py): applies the scale to a
  rendered LineCollection; called per-line by the render path
  (`_render_positioned_lines`, reached via `_render_text_local_with_bounds`
  and `_render_text_lines_by_pen`) before centering.
- Lines that already fit are never modified. If compression cannot fully
  resolve the overflow (limit too small), a WARNING is logged.

### Horizontal Text Alignment

Each text line is positioned horizontally within the label's inner content
area (`[margin, width - margin]`) according to `text_h_alignment`. Values
cascade line → label → job (default `center`; accepted on plates for schema
parity only). Enum `TextHAlignment` (`left`, `center`, `right`).

- `left`: the line's left-most point sits **precisely at the left margin**.
- `right`: the line's right-most point sits **precisely at the right margin**.
- `center`: the line is centered within the inner content area (existing
  behaviour; backward compatible default).
- `compute_horizontal_offset()` (resolution.py): pure offset math returning
  the target left-edge X for a rendered line; unknown values fall back to
  `center`.
- Applied per-line by the render path (`_render_positioned_lines`) after
  compression, before vertical stacking.
- When a line is wider than the inner area (e.g. compression disabled),
  `center` overflows symmetrically while `left`/`right` keep their aligned
  margin edge anchored and spill out the opposite side.

### Text-Hole Collision Avoidance

Rendered text lines are checked against drill holes via the circle-vs-AABB
closest-point gap in `geometry.circle_aabb_gap()`. The check is
**stroke-aware**: a (line, hole) pair collides when the geometric gap falls
below the per-line threshold

`threshold = 0.5 * (hole_cutter + text_cutter) + hole_text_collision_distance`

where `text_cutter` is the line's resolved cutter diameter and
`hole_cutter` is the boundary/hole cutter from `tools.json`
(`boundary_hole_cutter_size`, default 0.015, snapped to
`available_cutters`: next size down, else next size up). The first term is
the stroke floor at which the two cut strokes just touch, so near misses
that would make engraved strokes bleed together are collisions too; a gap
exactly equal to the threshold is safe. Detection runs in the label-local
y-up frame, shifted by `height / 2` to match export centering; Y-flip is
intersection-invariant.

- **Phase 1 (always on, observational):** `_detect_text_hole_collisions()`
  flags per-(line, hole) threshold violations and logs an ERROR with label
  id, line index, hole location, measured gap, and the required-clearance
  breakdown (clearance + stroke floor). `RenderedLabel.collision_detected`
  marks any render where a collision was found (even if a later phase fixed
  it) and `RenderedLabel.has_collisions` marks renders that still collide
  after resolution. Detection runs inside `render_label_to_plt` (and the
  collision-avoidance sweeps re-check via `_detect_text_hole_collisions()`).
- **Phase 2 (opt-in via `min_hole_margin`):** sweep `hole_margin` toward the
  floor (analytical — hole positions are pure functions of `hole_margin`);
  on success re-render from the adjusted label clone and log WARNING with
  before/after margins and the offending text line. (Any geometry-altering
  avoidance action — margin reduction or collision compression — always logs
  at WARNING and names the label id plus the affected text line.)
- **Phase 3 (opt-in via `max_h_compress`):** if margins cannot clear the
  overlap, sweep a uniform horizontal compression (`collision_compress` on
  `ResolvedLabel`, applied before margin-driven compression) up to the line
  budget floor `1 - max_h_compress`. Stacks on top of the Phase 2 floor.
- **Failure semantics (collisions are unacceptable):** every detected
  collision logs an ERROR naming the label id, offending text line, and hole.
  When no enabled phase clears a collision, `render_label_to_plt` adds full
  diagnostics (gap shortfalls, margin/compression state, recommendations) at
  ERROR and flags `has_collisions=True`. Either way the render is flagged
  `collision_detected=True`, and the job-level gate `assert_no_collisions()`
  — wired into `layout.generate_layout_with_bounds` (covering
  `vectorize.export_per_cutter_plts`) — raises `LabelRenderError` once every
  label has been rendered (so all per-label ERRORs print first), naming
  every offending label id. Avoidance repairing a collision does **not**
  excuse it: the jobspec must be revised. The `generate` CLI surfaces the
  abort as a non-zero exit code.
- Adjusted label clones propagate to downstream rendering via
  `RenderedLabel.source_label` (consumed by `layout.unroll_labels_with_rendered_bounds`).

### Job Specification Patterns

**Pattern 1: Explicit Labels List**
```yaml
job:
  job_name: "Batch 01"
  text_height: 0.5
  labels:
    - id: "label_1"
      count: 10
      content:
        - text: "Line 1"
        - text: "Line 2"
```

**Pattern 2: Root-Level Single Label** (auto-repeated via `count`)
```yaml
job:
  job_name: "Simple Labels"
  count: 20
  content:
    - text: "Single repeating label"
```

**Pattern 3: Replacement Text File Template** (EngraveLab/Vision Pro style)
```yaml
job:
  job_name: "Batch 02"
  labels:
    - id: "badge"                    # expands to badge_0000, badge_0001, ...
      replacement_text_file: data.txt  # relative to the job YAML directory
      replacement_text_delimiter: ";"  # optional, default ";"
      content:                       # optional placeholders (see below)
        - text: "PLACEHOLDER L1"
          text_height: 0.45
```

### Replacement Text Files (Badges / Multiples)

`plt_optimizer/generate/substitution.py` implements EngraveLab/Vision Pro
"badge" (a.k.a. "multiples") data-driven generation. A `LabelSpec` with
`replacement_text_file` is a template: **each line of the file produces one
label copy** (the file's line count determines the count; `count` must not
be set alongside it). Items within a file line are split on
`replacement_text_delimiter` (default `;`; must be a single special or
whitespace character, never newline/alphanumeric).

- **File format:** pure data — every line is a label copy (no comment
  syntax). CRLF/CR endings are normalized; a single trailing newline does
  not create a phantom copy; a UTF-8 BOM is stripped; items are used
  verbatim (no whitespace stripping). Blank lines render blank lines
  (WARNING logged). Loading failures raise `SubstitutionError` (a
  `ValueError` subclass).
- **`content` optional:** when omitted, every rendered line inherits
  label-level attributes. When present, each `text` value is a
  placeholder declaring that line's attributes (text_height, alignment,
  spacing, ...); the replacement item overrides only `text`.
- **Fewer items than template lines:** the copy renders only as many
  lines as items present (extra template lines dropped); the rendered
  block stays vertically centered (existing renderers center regardless
  of line count).
- **More items than template lines:** extra lines inherit label-level
  attributes (same as the no-content case).
- **Delimiter collisions:** items can never contain the delimiter (data
  is split on it), so users must choose a delimiter absent from their
  data — the EngraveLab constraint, enforced by construction.
- **Expansion:** `expand_job_spec(job, yaml_path)` runs between
  `parse_yaml()` and `resolve_job_spec()` (wired into `cli/generate.py`
  and `run_integration_test.py`). Each file line becomes a static
  `LabelSpec` with `count=1`, id suffix `_{index:04d}`, and replacement
  fields cleared. Static labels and root-level jobs pass through
  untouched; jobs without replacement labels return the same object.
- **Examples:** `examples/replacement_job.yaml` with
  `examples/replacement_text_sample.txt` / `replacement_text_assets.txt`.

### Plate-Space Toolpath Optimization
Generated toolpaths are optimized in the **plate frame** (post-rectpack device
coordinates) before any PLT is written, one routing problem per plate **and per
cutter** (`plt_optimizer/generate/plate_optimizer.py`). Because the generator
already knows each toolpath's kind, the `Profiler` is skipped entirely.

- **Text layers**: one TSP node per `TextChunkRecord` carried out of label
  rendering (label-local inches, +y up). The chunker is bypassed — the records
  *are* the nodes. `text_chunk_mode` (job-level, `"line"` default / `"word"`)
  sets granularity: `line` routes each whole line; `word` splits on whitespace
  (exact stroke membership via `ftext_renderer.render_text_line_ftext_with_words`,
  which partitions the whole-line render's contours by translation-invariant
  signature — bit-exact, no advance math).
- **Structural layers** (borders SP2 + holes SP3): the extracted HPGL is parsed
  and pushed through the shared core pipeline (`core.pipeline`
  `preprocess_document` → `chunk_document` → `optimize_and_reassemble`) with a
  hand-built `ProfileResult(is_structural=True)`; the structural chunker branch
  maps every path 1:1 to a block.
- **Re-emission**: `emit_layer_document` writes integer-unit HPGL
  (`PU`/`PD`/`AA`) re-selecting `SP` on every pen change. The generic
  `PLTWriter` cannot be used here — it hoists all headers (including pen
  selects) ahead of the geometry, which would leave generated content
  pen-unselected.
- **Transform chain** (verified against emitted files, plotter units): center
  text block to `height/2` → Y-mirror by the rendered bounds sum → rotate 90° CW
  if the packer placed the label sideways → translate by the packed slot. Text
  geometry is emitted vertex-exact; only coincident structural strokes are
  deduplicated.
- **Strategy**: `ParallelEnsembleStrategy` by default, `NearestNeighbor2Opt`
  under `--fast-mode` (mirrors the `optimize` CLI). `export_per_cutter_plts`
  takes `fast_mode` and `logger`; each layer logs baseline→optimized rapid
  travel at INFO.
- The post-write `_run_optimizer` (parse each file → profile → optimize) is
  **removed**; optimization now happens pre-write in plate space.

### Cascading Resolution
When a value is `None` at the TextLine/LabelSpec level, it inherits from the parent JobSpec. Cascade order for `hole_margin`: explicit label value → job value → default. Same precedence applies to `max_h_compress` (explicit 0.0 is honored, not treated as unset), `text_h_alignment` (explicit `center` is honored, not treated as unset), `min_hole_margin` (explicit 0.0 is honored; only `None` means unset), and `hole_text_collision_distance` (explicit 0.0 is honored; only `None` means unset, falling back to 0.15).

### Bin-Packing Rotation (`allow_rotation`)
`JobSpec.allow_rotation` (bool, default `True`) lets the `rectpack` bin
packer test both orientations (0°/90°) for every label instance.

- `_pack_best()` (layout.py) evaluates every `PACK_CONFIGS` heuristic twice
  (rotation off, then on) and ranks candidates by `(footprint, rotated_count)`
  with a float-tolerant footprint comparison. Labels therefore rotate only
  when rotation *strictly* improves the used bounding-box area; at equal
  footprints the all-horizontal layout always wins (backward compatible).
- Rotation is detected in `_extract_packed_plates()` by comparing
  `rect.width` against the *packing* width carried in the `rid` payload
  (rendered bounds in `generate_layout_with_bounds`, nominal otherwise) —
  never against the nominal `ResolvedLabel.width`, which can differ from
  what was actually packed.
- Rotated content is turned 90° **clockwise** at plate assembly:
  `vectorize.rotate_plt_content_90cw()` maps `(x, y) → (y_max − y, x − x_min)`
  over `PA`/`PU`/`PD` pairs and `AA` arc centers (sweep angles preserved —
  a pure rotation has positive determinant), normalizing the rotated bbox to
  the origin so the existing slot translation lands it in `[x, x+H] × [y, y+W]`
  with non-negative coordinates. Text, border and drill holes rotate together
  because they share one `plt_content` string.
- Unbounded-mode `LayoutFitError` wording is orientation-aware ("in either
  orientation"): a label that fails 24x16 upright may still fit sideways.
- Example fixture: `examples/rotation_demo_job.yaml` (pinned by
  `tests/test_layout.py::TestRotationDemoExample`) — a tight 24x10 scrap
  sheet where rotation-required, rotation-refused and opportunistic-rotation
  labels coexist; it aborts with `LayoutFitError` when rotation is disabled.

### Integration Points
- `parse_yaml(file_path)` returns a `JobSpec` ready for downstream bin-packing and rendering pipelines
- `expand_job_spec(job, yaml_path)` (substitution.py) must run immediately after `parse_yaml()` before `resolve_job_spec()` to flatten replacement-driven labels
- All numeric fields support Pydantic's `ge` (greater-than-or-equal) validators for safety
- Use `job.labels` or synthesize from root-level `content` + `count` when processing

## 7. CLI Surface (`optimize` / `generate` / `watch`)

The console script `plt-optimizer` is routed by `main.py` into three
subcommands (`plt_optimizer/cli/optimize.py`, `generate.py`, `watch.py`).
`main.py` must stay the single entry point (`[project.scripts]` in
`pyproject.toml` points at `main:main`).

### `optimize <input.plt>`
Single-file parse → profile (text vs. structural) → chunk → optimize →
reassemble → write. Flags: `-o/--output` (default `<stem>_optimized.plt`
beside the input), `--fast-mode` (NearestNeighbor2Opt only; default is
ParallelEnsemble with per-strategy benchmark logging), `-v/--verbose`,
`--log-dir` (default `./logs_optimize/`; writes `optimizer.log` +
`job_metrics.csv`). Prints one summary line to stdout unless `-v`.

### `generate <spec.yaml>`
Three-phase pipeline: `parse_yaml` → `expand_job_spec` → `resolve_job_spec` →
`export_per_cutter_plts` (bounds-aware packing + per-label rendering + plate-space
per-cutter optimization + split by cutter). Flags: `-o/--output` (default: spec's
parent dir; receives `plt/` and `pdf/`), `-v/--verbose`, `--no-plots` (skip
simple-outline PDF previews), `--default-plots` (opt-in color-coded `*_default.pdf`
rapid-travel plots), `--tools` (default `tools.json`; missing file → ideal
cutters), `--fast-mode` (plate-space routing via `NearestNeighbor2Opt` instead of
the default `ParallelEnsemble`). File names:
`<2-digit plate>_{text|bh}_<cutter>_<job_id>.<plt|pdf>` plus combined
`<plate>_all_<job_id>.pdf`. Logs go to `./logs_generate/generate.log`.
Collision aborts (`LabelRenderError`) and fit failures (`LayoutFitError`) exit
non-zero after full ERROR diagnostics.

### `watch --watch-dir <dir>`
Hot-folder daemon. Flags: `--watch-dir` (required), `--output-dir` (default
`./optimized`), `--log-dir` (default `./logs`), `--processed-dir` (archive
originals; without it originals are **deleted**), `--fast-mode`,
`--debug-save-files` (before/after PLTs + PNG plots under `<log-dir>/debug/`;
only with an explicit `--log-dir`), `--debounce-seconds` (default 2.0).
Behavioural invariants:
- Files are processed only after `debounce_seconds` of quiescence **and** after
  the OS file lock is released (`_is_file_locked` probe).
- Outputs are staged in `<output-dir>/.incomplete/` and moved into place with
  `os.replace` (fallback `shutil.move`) — consumers never see partial files.
- On failure, the input is copied to the output dir as `<stem>_unprocessed.plt`
  and removed from the watch dir; the job is logged with `status=failed`.
- `setup_parser()` is the single source of truth for watch flags (shared by the
  `main.py` router and `python -m plt_optimizer.cli.watch`); the tray app calls
  `run_watcher_from_config()` instead of the CLI layer.

### Python 3.8 / Windows 7 import constraint
Per section 5, Windows 7 support is **CLI-only**: the pre-built
`Ploptimizer.exe` installer and the system tray GUI are Windows 10+ only
(dropped for Win7). On Windows 7 the supported workflow is the headless
`watch`/`optimize` CLI on Python 3.8 without matplotlib (not installable on
3.8). Therefore `watch` (and the tray's watcher path) must remain importable
and runnable on Python 3.8 without matplotlib. The `generate` pipeline and its
modules (`plt_optimizer/generate/*`, which import numpy/matplotlib/vpype text
rendering) require Python 3.9+. Keep heavy generation imports out of any code
path the watch/optimize commands execute at startup:
- `main.py` builds all three subparsers at startup, so
  `plt_optimizer/cli/generate.py` stays import-light at module scope: pure
  Python imports (`schema`, `resolution`, `substitution`) are top-level, while
  the matplotlib-transitive ones (`layout`, `label_renderer`, `vectorize`) are
  imported lazily inside `run()`. Tests monkeypatch
  `plt_optimizer.generate.vectorize.export_per_cutter_plts` (the source module),
  not the CLI module attribute.
- Verify with a matplotlib import-blocker that `import main` and
  `plt-optimizer watch --help` succeed without matplotlib before changing CLI
  import structure.
