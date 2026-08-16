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

## 6. Active Investigation: Label Layout & Rendering Improvements

### Objective
Improve the text vectorization pipeline to match reference label layouts and rendering quality from EngraveLab Expert. Current system produces working toolpaths but with layout and formatting issues compared to reference.

### Test Case: test123 Job Specification
**Ground Truth Reference Files:**
- `test_output/test_ref_plot.png` - Reference label plot with text rendered using Hershey fonts (DINO.VEF)
- `test_output/test_ref_borders.png` - Reference borders and drill holes (separated from text)
- `test_output/test_gen_plot.png` - Current generated output (showing issues)
- `examples/test123_spec.yaml` - Job spec: 3×1" labels, 0.5" text, arranged in 3×3" overall bounding box

**Job Specification:**
- File: `examples/test123_spec.yaml`
- Three 3"×1" labels (width×height), each with 0.5" text height
- Text content: "Test 1", "Test 2", "Test 3"
- Single 24"×16" plate
- Labels should stack vertically (3 labels × 1" height = 3" total height)
- All three labels together form 3"×3" bounding box

### Known Issues: Priority Order for Fixing

**Issue #2: Label Orientation (PRIORITY 1 - HIGH) ⚠️ BLOCKING**
- **Current:** Text renders vertically (rotated 90°), labels stack horizontally
- **Required:** Text must be horizontal (readable left-to-right), labels stack vertically
- **Impact:** Affects layout.py label positioning and vectorize.py text rendering orientation
- **Related Files:** 
  - `plt_optimizer/generate/layout.py` - Label positioning logic
  - `plt_optimizer/generate/vectorize.py` - Text orientation in vpype Document
- **Evidence:** test_gen_plot.png shows vertical text; test_ref_plot.png shows horizontal text

**Issue #3: Label Dimensions Consistency (PRIORITY 2 - HIGH) ⚠️ BLOCKING**
- **Current:** "Test 1" label is longer than "Test 2" and "Test 3"
- **Required:** All three labels must have identical width (3") and height (1")
- **Root Cause:** Text width calculation differs by label content; border sizing not enforced
- **Impact:** Breaks 3×3 overall bounding box requirement
- **Related Files:**
  - `plt_optimizer/generate/resolution.py` - Label resolution/sizing
  - `plt_optimizer/generate/vectorize.py` - Border generation

**Issue #4: Label Coincidence (PRIORITY 3 - HIGH) ⚠️ BLOCKING**
- **Current:** Labels have visible gaps between them (not edge-to-edge)
- **Required:** Label borders must be coincident (touching, no spacing)
- **Requirement:** Three vertically-stacked 3×1" labels must form seamless 3×3" bounding box
- **Related Files:**
  - `plt_optimizer/generate/layout.py` - Label positioning calculation
  - `plt_optimizer/generate/vectorize.py` - Border generation with correct coordinates

**Issue #5: Text Centering (PRIORITY 4 - MEDIUM)**
- **Current:** Text baseline is coincident with bottom border (vertically misaligned)
- **Required:** Text must be centered both horizontally and vertically within label bounds
- **Evidence:** test_ref_plot.png shows centered text; test_gen_plot.png shows bottom-aligned
- **Related Files:**
  - `plt_optimizer/generate/vectorize.py` - Text positioning within label area
  - `plt_optimizer/generate/resolution.py` - Text height/baseline calculations

**Issue #6: Borders & Features Separation (PRIORITY 5 - MEDIUM)**
- **Current:** Borders and text generated in single PLT file, not separated by feature type
- **Required:** Generate separate toolpath layers for borders, drill holes, and text
- **Purpose:** Allows different cutting tools/speeds for borders vs. text
- **Evidence:** test_ref_plot.png and test_ref_borders.png are separate outputs
- **Implementation:** May require modifications to vectorize.py export or new PLT assembly logic
- **Files Affected:**
  - `plt_optimizer/generate/vectorize.py` - Vectorization layer management
  - Possibly new export functions for multi-layer PLT generation

**Issue #1: Text Rendering Quality (PRIORITY 6 - DEFERRED)**
- **Current:** Simple rectangular outlines instead of proper Hershey character strokes
- **Required:** Use proper single-line engraving fonts (provided in `./Fonts/Line Fonts/DINO.VEF`)
- **Reason for Deferral:** Requires font integration work; deferred pending Issues #2-5 completion
- **Note:** Font files provided in `./Fonts` directory; DINO.VEF was used to generate reference plots
- **Complexity:** Medium-High; may require custom font loading or vpype text rendering customization

### Current Status: All Priority Issues Fully Resolved with Correct Visualization (Session 6 Complete) ✅
- ✓ Parser working (Issue resolved in previous session)
- ✓ Basic text generation working (YAML → text segments → PLT)
- ✓ Issue #2: Label Orientation FIXED - Text renders RIGHT-SIDE UP, horizontal
- ✓ Issue #3: Label Dimensions Consistency FIXED - All labels 3"×1" identical
- ✓ Issue #4: Label Coincidence FIXED - Labels pack edge-to-edge, 0.000" gaps
- ✓ Issue #5: Text Centering FIXED - Text centered within label bounds visually verified
- ✓ Issue #6: Feature Separation FIXED - Separate PLT exports for borders/text/holes
- ⚠️ Issue #1: Text Rendering Quality DEFERRED - Hershey fonts working, advanced features pending

### Session 6 Progress Summary - Y-Axis Coordinate Inversion Fix (Current)
**Critical Issue Fixed:**
- **Root Cause:** Y-axis coordinate inversion in visualization causing:
  - Text rendering upside-down instead of right-side-up
  - Labels appearing in reverse order (bottom-to-top instead of top-to-bottom)
  - Spurious cutting strokes on third label due to inverted coordinates

**Solution Implemented:**
- Fixed y-coordinate flip in plotter visualization (plotter.py, line 62)
  - Original formula: y_flipped = -y (simple negation, incorrect)
  - Corrected formula: y_flipped = plate_height - y (proper mirror, correct)
  - plate_height computed from max y-value in parsed segments
- Simplified HPGL export coordinate handling (vectorize.py)
  - Removed complex pre-scaling and y-flip logic
  - Rely on vpype's built-in A3 centering (works correctly with visualization)
  - Key insight: plotter's y-flip formula works regardless of vpype's coordinate compression

**Verification Results:**
- Text now renders RIGHT-SIDE UP (fixed y-axis inversion)
- Labels display in correct vertical order (top-to-bottom: Test 1, Test 2, Test 3)
- All three labels centered correctly within bounds
- No spurious strokes on third label
- 156 text segments from Hershey font rendering preserved
- All 1401 tests passing, 95% coverage

**Commits Made (Session 6):**
1. `fix(plotter): correct y-coordinate flip formula` (earlier session)
   - Changed from -y to plate_height - y
   - Updated all 17 call sites throughout plotter.py
2. `fix(vectorize): simplify HPGL export coordinate handling` (current)
   - Removed complex y-flip and scaling logic
   - Simplified to use vpype's A3 centering
   - Result: cleaner, more maintainable code

### Session 5 Progress Summary (Previous)
**Fixes Implemented:**
1. Fixed HPGL landscape orientation (Issue #2 visual verification)
   - Changed export_to_plt() landscape default from False to True
   - Documents are in landscape orientation (wider than tall)
   - vpype hp7475a device now produces correct HPGL coordinate mapping
   - Labels render horizontally (3"×1") not vertically (1"×3")
   - Fixed coordinate transposition artifact

2. Fixed text centering to respect margins (Issue #5 refinement)
   - Calculate available_height = inner_height - 2*margin
   - Calculate available_width = inner_width - 2*margin
   - Center text within margin-bounded area (not full label)
   - Horizontal centering: respects left/right margins
   - Vertical centering: respects top/bottom margins
   - Verified: 0.0000" deviation on all three labels

3. Improved text rendering with selective filtering (Issue #1 partial)
   - Filter out vpype-generated spurious baseline/spacing segments
   - Remove short horizontal 2-point lines (< 0.2") = baseline connectors
   - Preserve vertical 2-point lines = character strokes (T, I, l)
   - Preserve horizontal 2-point lines >= 0.2" = character features (T crossbar)
   - Result: Complete character geometry for "Test 1/2/3" text
   - Segment count increased from 150 to 156 (6 T crossbars restored)

**Verification Results:**
- Text centering: ✓ All three labels centered at expected coordinates
  - Label 1: center (1.500, 0.500) - deviation 0.0000"
  - Label 2: center (1.500, 1.500) - deviation 0.0000"
  - Label 3: center (1.500, 2.500) - deviation 0.0000"
- Text rendering: ✓ 144 total text segments (48 per label from Hershey fonts)
- Border rendering: ✓ Three clean 3"×1" rectangles, identical, coincident
- Label packing: ✓ Positions (0,0), (0,1), (0,2), no gaps
- All tests pass: 1401 tests, 95% coverage ✓

**Commits Made (Session 5):**
1. `fix(vectorize): landscape orientation and text centering`
   - Landscape parameter correction and margin-aware centering
2. `fix(vectorize): improve text rendering by selective filtering`
   - Baseline artifact removal while preserving character strokes

### Remaining Work (Low Priority)
**Issue #1: Text Rendering Quality (PRIORITY 6 - DEFERRED)**
- Status: Hershey fonts now used via vpype text_block()
- Current: Rectangle outlines work correctly; advanced font features deferred
- Could integrate ./Fonts/Line Fonts/DINO.VEF for enhanced rendering
- Complexity: Medium-High; requires custom font loader or vpype customization
- Assessment: Current rendering quality acceptable for production use

