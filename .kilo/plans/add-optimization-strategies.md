# Plan: Additional TSP Optimization Strategies for PLT-Optimizer

## Context

The current `NearestNeighbor2OptStrategy` suffers from the classic nearest-neighbor greedy problem: chunks left behind requiring backtracking. The 2-opt refinement helps but is limited to reversing existing segments.

Goal: Implement additional optimization strategies and update diagnostics to visualize all strategies.

---

## Strategies to Implement

### 1. **Insertion Heuristic Strategy** (Recommended First)

**Why:** Unlike nearest-neighbor which builds forward, insertion heuristics start with a tour edge and insert remaining nodes at locally optimal positions. This inherently avoids the "left-behind" problem because every insertion considers where to place each chunk in the *existing* tour.

**Algorithm:**
1. Start with two connected endpoints (closest pair)
2. For each unvisited block, find the position (between which two consecutive blocks) where inserting it causes minimal distance increase
3. Insert at that best position
4. Repeat until all blocks visited

**Variants to consider:** Cheapest Insertion (picks the block that adds minimum cost), Nearest Insertion (inserts block closest to current tour), Farthest Insertion (starts with outlier chunks first)

### 2. **Christofides-Serdyukov Algorithm**

**Why:** Has a formal 3/2 approximation guarantee for metric TSP, which applies since Euclidean distance satisfies triangle inequality. Also builds MST-based structure that naturally clusters nearby blocks.

**Algorithm:**
1. Build Minimum Spanning Tree (MST) of all block endpoints
2. Find vertices with odd degree in MST
3. Compute minimum-weight perfect matching on odd-degree vertices
4. Combine MST + matching edges to form Eulerian multigraph
5. Find Eulerian tour, then shortcut to Hamiltonian

### 3. **Genetic Algorithm / Evolutionary Strategy**

**Why:** Population-based approaches explore solution space more broadly, reducing likelihood of getting stuck in local minima that plague greedy algorithms.

**Key components:**
- Chromosome encoding: permutation of block indices with direction bits
- Fitness function: total travel distance
- Selection: tournament or roulette wheel
- Crossover: Order crossover (OX), PMX, or cycle crossover
- Mutation: swap, inversion, insertion mutations
- Elitism to preserve best solutions

### 4. **Simulated Annealing Strategy**

**Why:** Global search technique that can escape local minima by accepting worse solutions with probability that decreases over time.

**Key parameters:**
- Initial temperature
- Cooling rate
- Number of iterations per temperature
- Acceptance criterion (Metropolis)

---

## Implementation Plan

### Phase 1: Add New Strategy Classes to `optimizer.py`

```
1. InsertionHeuristicStrategy
   - Implement cheapest_insertion_optimize() method
   - Override _calculate_block_cost to work with insertion positions

2. ChristofidesStrategy
   - Build MST using Prim's or Kruskal's algorithm
   - Find odd-degree vertices, compute perfect matching (Hungarian algorithm)
   - Combine edges and find Eulerian tour

3. GeneticAlgorithmStrategy
   - Implement population initialization
   - Selection, crossover, mutation operators
   - Fitness evaluation and elitism

4. SimulatedAnnealingStrategy
   - Temperature schedule management
   - Neighbor generation (segment reversal, block swap)
   - Metropolis acceptance criterion
```

### Phase 2: Update `run_diagnostics.py`

Add command-line flag `--all-strategies` or `--strategy <name>`:

1. Create a registry dict mapping strategy names to classes
2. When running demo mode:
   - Run NoOp (baseline) → produces "before" plot
   - For each non-NoOp strategy, run optimization and produce `<prefix>_<strategy>.png`
3. Compare output:
   - Show side-by-side or grid of all strategy results
   - Report distance savings for each

### Phase 3: Testing

- Add unit tests for each new strategy (identity validation)
- Benchmark comparison between strategies on sample data
- Ensure all strategies produce valid OptimizationResult objects

---

## Files to Modify

| File | Changes |
|------|---------|
| `plt_optimizer/core/optimizer.py` | Add 4 new strategy classes, register in engine |
| `examples/run_diagnostics.py` | Add `--strategy` flag, multi-strategy output support |
| Tests (new or existing) | Unit tests for each strategy |

---

## Implementation Notes

### Commit Strategy
Each strategy implemented in its own commit with:
- Strategy class implementation
- Unit tests for the new strategy
- Documentation update if needed

Order: Insertion Heuristic → Christofides-Serdyukov → Simulated Annealing → Genetic Algorithm (GA)

### GA Approach
Implement both options:
1. **Pure Python fallback**: Custom GA implementation using only standard library
2. **DEAP integration**: Use DEAP if available, fall back to pure Python

The code structure should detect DEAP availability and use it when present.

### Output Naming Convention
Each strategy produces its own plot file with strategy name suffix:
- `{prefix}_after_nn2opt.png` (current)
- `{prefix}_after_insertion.png`
- `{prefix}_after_christofides.png`
- `{prefix}_after_sa.png`
- `{prefix}_after_genetic.png`

### New Command-Line Options
```bash
--all-strategies    # Run all strategies and generate individual plots
--strategy <name>   # Run specific strategy only
```

Example output when using `--all-strategies`:
```
optimized_before.png           (baseline - no optimization)
optimized_after_nn2opt.png     (NearestNeighbor + 2-opt)
optimized_after_insertion.png  (Cheapest Insertion Heuristic)
optimized_after_christofides.png (Christofides-Serdyukov Algorithm)
optimized_after_sa.png         (Simulated Annealing)
optimized_after_genetic.png    (Genetic Algorithm)
```

### Strategy Implementation Details

**1. Cheapest Insertion Heuristic:**
- Start with the closest pair of endpoints
- Iteratively insert remaining blocks at cheapest position
- Consider both entrance/exit for each block insertion point
- Time complexity: O(n²)

**2. Christofides-Serdyukov Algorithm:**
- Build MST using Prim's algorithm (O(n²))
- Find odd-degree vertices in MST
- Compute minimum-weight perfect matching using Hungarian method
- Combine and shortcut to valid tour

**3. Simulated Annealing:**
- Initial temperature: 10000
- Cooling rate: 0.9995
- Neighbor generation: random segment reversal or block swap
- Acceptance: Metropolis criterion

**4. Genetic Algorithm (with DEAP fallback):**
- If DEAP available: use it for clean evolutionary operators
- Otherwise: pure Python implementation
- Population size: 50
- Generations: 200
- Mutation rate: 0.1
- Elitism: top 2 solutions