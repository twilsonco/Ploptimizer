# Plan: Intra-Chunk Path Optimization

## Context

The current architecture chunks stroke paths into `MacroBlock`s based on large rapid-travel jumps, then optimizes **between** blocks using `NearestNeighbor2OptStrategy`. Within each block, strokes are currently only reversed (order + direction) when the whole block is traversed in reverse.

This plan adds **intra-chunk optimization**: optimizing the order and direction of stroke paths *within* each chunk while keeping the chunk's entrance and exit points fixed. This reduces internal rapid travel without affecting inter-chunk routing.

## Goal

Add optional intra-chunk TSP optimization that:
- Keeps block `entrance` (first path's first segment start) **fixed** as start point
- Keeps block `exit` (last path's last segment end) **fixed** as end point
- Reorders and/or reverses individual `StrokePath`s within the block to minimize internal travel

## Architecture

### Option A: New IntraChunkOptimizer class (Recommended)

Create a new optimizer in `plt_optimizer/core/intra_chunk_optimizer.py` that:
1. Takes a single `MacroBlock`
2. Treats each `StrokePath` as a node with entrance/exit points
3. Uses similar `NearestNeighbor2OptStrategy` to find optimal path order/direction
4. Returns optimized paths (still within the block, respecting entrance/exit constraint)

**Advantages:**
- Single responsibility: clear separation between inter-chunk and intra-chunk optimization
- Testable in isolation
- Can be disabled independently via config flag

### Option B: Integrate into Reassembler

Add intra-chunk logic directly to `Reassembler._reverse_block_paths()` or a new method.

**Disadvantages:**
- Violates single responsibility
- Harder to test and disable

## Implementation Details

### 1. New File: `plt_optimizer/core/intra_chunk_optimizer.py`

```python
@dataclass(frozen=True)
class PathTraverseState:
    """Represents whether a StrokePath should be traversed forward or reverse."""
    path_index: int
    reversed: bool
    entrance: Coordinate  # Actual entrance after considering reversal
    exit: Coordinate      # Actual exit after considering reversal

class IntraChunkStrategy(ABC):
    """Abstract base for intra-chunk optimization strategies."""

    @abstractmethod
    def optimize_block(
        self,
        paths: Tuple[StrokePath, ...],
        fixed_entrance: Coordinate,
        fixed_exit: Coordinate,
    ) -> List[Tuple[int, bool]]:  # List of (path_index, should_reverse)
        """Optimize path order/direction within a block.

        Returns:
            List of (original_path_index, reversed) tuples in optimized order.
        """
        ...

class NearestNeighborIntraStrategy(IntraChunkStrategy):
    """Nearest neighbor + 2-opt for intra-chunk optimization."""

    def optimize_block(
        self,
        paths: Tuple[StrokePath, ...],
        fixed_entrance: Coordinate,
        fixed_exit: Coordinate,
    ) -> List[Tuple[int, bool]]:
        # Similar to NearestNeighbor2OptStrategy but constrained:
        # - Must start from fixed_entrance
        # - Must end at fixed_exit
        ...
```

### 2. New Config Field

Add `enable_intra_chunk_optimization: bool = True` to `ChunkerConfig`:

```python
@dataclass
class ChunkerConfig:
    threshold_multiplier: float = 1.5
    min_block_size: int = 1
    enable_intra_chunk_optimization: bool = True
```

### 3. Modify `MacroBlock` or Reassembler

Option A (cleaner): Add method to MacroBlock that returns optimized paths:

```python
@dataclass(frozen=True)
class MacroBlock:
    # ... existing fields ...

    def get_optimized_paths(
        self,
        strategy: Optional[IntraChunkStrategy] = None,
    ) -> List[StrokePath]:
        """Return stroke paths in optimized order with fixed entrance/exit."""
```

Option B (more flexible): Handle in `Reassembler.reassemble()` by:
1. For each block, checking config flag
2. If enabled, calling intra-chunk optimizer before applying inter-chunk direction

### 4. Integration Point

The optimization pipeline becomes:

```
Parser → Profiler → Chunker → [Intra-Chunk Opt] → Inter-Chunk Opt → Reassembler → Writer
                              ↑                         ↑
                    Optional (config flag)      Always runs
```

**Integration in `Reassembler.reassemble()`:**
```python
def reassemble(
    self,
    original_document: PLTDocument,
    blocks: List[MacroBlock],
    optimization_result: OptimizationResult,
    intra_chunk_strategy: Optional[IntraChunkStrategy] = None,  # NEW param
) -> PLTDocument:
```

Or better, add an `OptimizerConfig` object that holds both strategies.

### 5. Reassembler Changes

The `_reverse_block_paths()` currently reverses all paths when a block is reversed globally. With intra-chunk optimization:

1. Paths within block are already in optimal order with proper direction
2. If block itself is reversed (inter-chunk), we need to flip the entire optimized sub-tour
3. This requires rethinking how reversal works

**Alternative approach**: Store per-path direction info in a new dataclass returned by intra-chunk optimizer, then apply during reassembly.

## Files to Create/Modify

| File | Change |
|------|--------|
| `plt_optimizer/core/intra_chunk_optimizer.py` | **CREATE** - New intra-chunk optimization module |
| `plt_optimizer/core/chunker.py` | Add `enable_intra_chunk_optimization` to `ChunkerConfig` |
| `plt_optimizer/core/optimizer.py` | Potentially rename/refactor if integrating both levels |
| `plt_optimizer/core/reassembler.py` | Update to accept and apply intra-chunk optimization |
| `tests/test_intra_chunk_optimizer.py` | **CREATE** - Unit tests for new module |

## Test Plan

1. **Identity test**: Input PLT → parse → chunk → intra-opt (enabled) → reassemble → write → re-parse should produce same geometry
2. **Improvement test**: With known paths, verify intra-chunk optimization reduces internal travel distance
3. **Opt-out test**: When `enable_intra_chunk_optimization=False`, behavior unchanged
4. **Edge cases**:
   - Single path in block (no optimization possible)
   - Two paths in block (only 2 permutations to check)
   - Block with all same-direction paths

## Questions for User

1. **Integration point**: Should intra-chunk optimization happen:
   - A) Before inter-chunk optimization (optimize each block first, then optimize block order)?
   - B) After inter-chunk optimization (within the already-ordered blocks)?
   - C) Both independently? (run twice - once before, once after reordering)

2. **API preference**: Should intra-chunk be:
   - A) Part of `ChunkerConfig` as a boolean flag + strategy?
   - B) Separate `IntraChunkOptimizer` class controlled separately?

3. **Metrics**: Should we track separate metrics for intra-chunk savings vs inter-chunk savings in the CSV logger?

## Estimated Complexity

- New module with ~200 lines of core TSP logic
- Integration changes to reassembler (~20 lines)
- Config update (~5 lines)
- Tests (~150 lines)

The constraint of fixed entrance/exit makes this a **Graphical Traveling Salesperson Problem with fixed endpoints** - well-studied, tractable with the same nearest-neighbor + 2-opt approach.