# Plan: Add Same-Row Preference to Block Optimization

## Objective
Modify `NearestNeighbor2OptStrategy` to prefer moving to blocks/chunks on similar y-values (same row) during greedy selection.

## Implementation Details

### 1. Modify `NearestNeighbor2OptStrategy.__init__`

Add optional parameter:
```python
def __init__(self, same_row_preference: float = 1.0) -> None:
```

Store as instance attribute:
```python
self._same_row_preference = same_row_preference
```

### 2. Modify `_calculate_block_cost` (lines 161-168)

Add y-difference penalty to cost calculation:

**Current:**
```python
cost_to_entrance = math.sqrt(
    (to_entrance[0] - from_pos[0]) ** 2
    + (to_entrance[1] - from_pos[1]) ** 2
)
```

**Proposed:**
```python
dx = to_entrance[0] - from_pos[0]
dy = to_entrance[1] - from_pos[1]
base_distance = math.sqrt(dx ** 2 + dy ** 2)
y_penalty = (self._same_row_preference - 1.0) * abs(dy)
cost_to_entrance = base_distance + y_penalty
```

Same modification for `cost_to_exit`.

**Note:** When `same_row_preference=1.0` (default), no penalty is applied (backward compatible). Values > 1.0 increase cost for y-differences, making optimizer prefer same-row blocks.

### 3. Update `_greedy_nearest_neighbor_from_start`

This method also uses `_calculate_block_cost`. It must pass `from_pos` correctly - verify it receives the correct position after each block is added to tour.

## Files to Modify
- `plt_optimizer/core/optimizer.py`: Add parameter and modify cost calculation

## Affected Methods
Both greedy methods call `_calculate_block_cost`, so the y-penalty will apply uniformly:
1. `_greedy_nearest_neighbor` (line 393) - used when initial_position is provided
2. `_greedy_nearest_neighbor_from_start` (line 518) - used for candidate evaluation loop

Note: `_find_nearest_origin_endpoints` uses raw `math.sqrt` but only for finding starting candidates, not for tour optimization.

## Testing Considerations
1. Unit test: Verify default behavior unchanged when `same_row_preference=1.0`
2. Unit test: Verify y-penalty applied correctly for various preference values
3. Integration test: Optimize sample blocks with different row preferences and verify horizontal bias in traversal order

## Questions
- What default value should we use? User specified 1.0 (no penalty) as backward-compatible default.
- Should there be an upper bound validation on the parameter? (e.g., max 5.0 to avoid extreme behavior)