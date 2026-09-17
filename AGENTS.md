# AGENTS.md - System Instructions for AI Coding Assistants

## Role & Core Philosophy
You are an expert Principal Software Engineer acting as an autonomous agent in this repository. Your primary goal is to build `PLT-Optimizer`, a deterministic, cross-platform Python tool for optimizing geometric toolpaths. 

Prioritize reliability, mathematical precision, and strictly typed code over speed of delivery. Do not guess or hallucinate logic—if an implementation detail regarding HPGL/PLT parsing or Traveling Salesperson algorithms is ambiguous, stop and ask the user for clarification.

## 1. Coding Style & Standards
We adhere strictly to the **Ruff / Black** formatting standards and modern Python paradigms.
* **Strict Typing:** Every function, class, and method must have complete PEP 484 type hints. Run type checks (e.g., via `mypy` or `pyright` rules) before finalizing code.
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

**LabelAttributes** (extends TextAttributes, cascades to LabelSpec only):
- `width`, `height`, `margin`: Label dimensions & safety margins
- `hole_margin`: Distance from hole edge to label edge (cascades: label → plate → job)
- `holes`: List of `HoleSpec` objects (diameter + location enum)

### Key Classes

| Class | Purpose | Validation Rules |
|-------|---------|------------------|
| `JobSpec` | Root job container | Requires either `labels` list OR root-level `content` (mutually exclusive) |
| `LabelSpec` | Individual label definition | `count >= 1`, `content` (min 1 TextLine) |
| `TextLine` | Text content unit | Requires non-empty `text` string |
| `PlateSpec` | Physical sheet definition | All dimensions `>= 0`, includes `clearance_padding` |
| `HoleSpec` | Drilled hole definition | Diameter + location (8 enum values: corners + edges) |
| `parse_yaml()` | Entry point | Returns validated `JobSpec` or raises `ValueError` |

### Horizontal Text Compression

Over-wide text lines are uniformly compressed horizontally (glyphs + spacing
scale together; Y is untouched) so they respect the label margins. This is
**opt-in** via `max_h_compress` in [0, 1] (default 0.0 = disabled), cascading
line → label → job (accepted on plates for schema parity only).

- `compute_horizontal_scale()` (resolution.py): pure scale-factor math,
  clamped to `[1 - max_h_compress, 1.0]`.
- `compress_line_to_width()` (label_renderer.py): applies the scale to a
  rendered LineCollection; called per-line by both render paths
  (`_render_text_local` and `vectorize._render_text`) before centering.
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
- Applied per-line by both render paths (`_render_text_local` and
  `vectorize._render_text`) after compression, before vertical stacking.
- When a line is wider than the inner area (e.g. compression disabled),
  `center` overflows symmetrically while `left`/`right` keep their aligned
  margin edge anchored and spill out the opposite side.

### Text-Hole Collision Avoidance

Rendered text lines are checked against drill holes (circle-vs-AABB via the
closest-point gap in `geometry.circle_aabb_gap()`; tangency is not a
collision). Detection runs in the label-local y-up frame, shifted by
`height / 2` to match export centering; Y-flip is intersection-invariant.

- **Phase 1 (always on, observational):** `_detect_text_hole_collisions()`
  flags per-(line, hole) penetrations and logs a WARNING with label id, line
  index, hole location, and measured gap. `RenderedLabel.has_collisions`
  marks renders that still collide after resolution. `vectorize.py` calls the
  observational `log_text_hole_collisions()` per label.
- **Phase 2 (opt-in via `min_hole_margin`):** sweep `hole_margin` toward the
  floor (analytical — hole positions are pure functions of `hole_margin`);
  on success re-render from the adjusted label clone and log WARNING with
  before/after margins. (Any geometry-altering avoidance action — margin
  reduction or collision compression — always logs at WARNING.)
- **Phase 3 (opt-in via `max_h_compress`):** if margins cannot clear the
  overlap, sweep a uniform horizontal compression (`collision_compress` on
  `ResolvedLabel`, applied before margin-driven compression) up to the line
  budget floor `1 - max_h_compress`. Stacks on top of the Phase 2 floor.
- **Failure semantics:** `LabelRenderError` (with diagnostics) is raised only
  when `min_hole_margin` is set and resolution failed. Compression-only
  failure logs a WARNING and returns the unmodified render with
  `has_collisions=True` (top/bottom hole collisions are geometrically
  unfixable by horizontal compression).
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

### Cascading Resolution
When a value is `None` at the TextLine/LabelSpec level, it inherits from the parent JobSpec. Cascade order for `hole_margin`: explicit label value → job value → default. Same precedence applies to `max_h_compress` (explicit 0.0 is honored, not treated as unset), `text_h_alignment` (explicit `center` is honored, not treated as unset), and `min_hole_margin` (explicit 0.0 is honored; only `None` means unset).

### Integration Points
- `parse_yaml(file_path)` returns a `JobSpec` ready for downstream bin-packing and rendering pipelines
- All numeric fields support Pydantic's `ge` (greater-than-or-equal) validators for safety
- Use `job.labels` or synthesize from root-level `content` + `count` when processing
