# Fix HPGL Arc Command Parsing in PLT-Optimizer

## Problem Statement

HPGL arc commands (AA - Arc Absolute, AR - Arc Relative, CI - Circle) are being parsed incorrectly. Curves are flattened to straight lines or discarded entirely because the parser doesn't understand compound path+arc commands like `PD;AA...`.

**Example**: The file `1-inch-circle.plt` should plot a circle but plots as a single dot.

## Root Cause

The tokenizer splits compound commands into separate tokens:
- Input: `PD;AA1016.000,1016.000,90.000;`
- Token 1: `PD;` (parsed as empty PD - no coordinates)
- Token 2: `AA1016.000,1016.000,90.000;` (treated as unknown header command, discarded)

## HPGL Arc Command Reference

| Command | Format | Description |
|---------|--------|-------------|
| AA | `PD;AAcx,cy,a;` | Arc Absolute - clockwise arc from current pos to computed endpoint on circle centered at (cx,cy) with sweep angle a degrees |
| AR | `PD;ARcx,cy,a;` | Arc Relative - counter-clockwise arc |
| CI | `CIx,y,r;` | Circle - complete circle |

**AA Command Math**:
- Current position: P1 = (x1, y1)
- Center: C = (cx, cy)
- Radius: r = distance(P1, C)
- Start angle: θ_start = atan2(y1-cy, x1-cx) in radians
- Sweep clockwise by a degrees → Δθ = -a × π/180
- End position P2:
  ```
  P2.x = cx + r * cos(θ_start + Δθ)
  P2.y = cy + r * sin(θ_start + Δθ)
  ```

## Implementation Plan

### Phase 1: Data Model Extensions (models.py)

Add new arc segment class to represent HPGL arcs:

```python
@dataclass(frozen=True)
class ArcSegment:
    """An arc segment in a stroke path.

    Attributes:
        start: Starting coordinate of the arc.
        end: Ending coordinate of the arc (computed from center + angle).
        center: Center of the arc circle.
        sweep_angle: Sweep angle in degrees (+ = clockwise, - = counter-clockwise).
        is_cutting: True if pen was down during this segment.
    """
    start: Coordinate
    end: Coordinate
    center: Coordinate
    sweep_angle: float  # positive = clockwise
    is_cutting: bool

    @property
    def radius(self) -> float:
        """Calculate the radius of the arc."""
        return self.start.distance_to(self.center)
```

Update `StrokePath` to hold both line segments and arc segments (use a Union type or separate tuple).

### Phase 2: Parser Changes (parser.py)

Modify `_build_document()` to handle compound commands:

1. Detect PD/PU followed by AA, AR, or CI in same token or immediately next tokens
2. When `PD;AAcx,cy,a` is found:
   - Use current position as arc start point
   - Compute end point using geometry formula above
   - Create ArcSegment with center=(cx, cy), sweep_angle=a
3. Handle standalone AA/AR/CI after PD; similarly

Modify `_extract_coordinates()` to return both coordinates and any trailing arc command parameters.

### Phase 3: Writer Changes (writer.py)

Add `write_arc_segment()` method that outputs:
- Format: `PD;AA{cx},{cy},{angle};`
- Use the stored center, sweep_angle, and compute endpoint

Update `PLTWriter.write_file()` to handle both StrokeSegment and ArcSegment.

### Phase 4: Plotter Changes (plotter.py)

Modify arc rendering in `plot_plt_document()`:

Option A - Convert arcs to line approximations for plotting:
```python
def _arc_to_points(arc, num_segments=32):
    """Sample points along an arc for linear approximation."""
    # Sample num_segments+1 points from start to end angle
    theta_start = atan2(arc.start.y - arc.center.y, arc.start.x - arc.center.x)
    delta_theta = -arc.sweep_angle * pi / 180 / num_segments
    return [Coordinate(
        arc.center.x + arc.radius * cos(theta_start + i*delta_theta),
        arc.center.y + arc.radius * sin(theta_start + i*delta_theta)
    ) for i in range(num_segments+1)]
```

Option B - Use matplotlib patches.Arc directly (requires keeping ArcSegment in plot data).

### Phase 5: Testing

Add test cases to `test_parser.py`:
- Parse AA commands from string
- Parse compound PD;AA sequences
- Verify arc endpoint calculation matches expected values

Add tests to `test_writer.py`:
- Round-trip arc segments through write → parse cycle

Add visual regression test using known-good PLT files with arcs.

## Files to Modify

1. `/home/haiiro/dev/PLT-Optimizer/plt_optimizer/core/models.py` - Add ArcSegment class
2. `/home/haiiro/dev/PLT-Optimizer/plt_optimizer/core/parser.py` - Handle compound arc commands
3. `/home/haiiro/dev/PLT-Optimizer/plt_optimizer/core/writer.py` - Output arc commands correctly
4. `/home/haiiro/dev/PLT-Optimizer/plt_optimizer/diagnostics/plotter.py` - Render arcs visually
5. `/home/haiiro/dev/PLT-Optimizer/tests/test_parser.py` - Add arc parsing tests
6. `/home/haiiro/dev/PLT-Optimizer/tests/test_writer.py` - Add arc writing tests

## Verification Plan

1. Run `python examples/run_diagnostics.py 1-inch-circle.plt`
   - Before fix: single dot at center
   - After fix: proper circle visible

2. Run `python examples/run_diagnostics.py hello-world.plt`
   - Before fix: only partial letters visible
   - After fix: "hello world" text fully rendered with curves

3. Run full test suite: `uv pytest tests/ -v`

4. Verify identity validation still passes for non-arc files (no regression)