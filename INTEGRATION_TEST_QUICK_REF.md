# Integration Test Quick Reference

## Running the Test

```bash
cd /Users/haiiro/NoSync/PLT-Optimizer
source .venv/bin/activate
python run_integration_test.py
```

## What Gets Tested

### Phase 1: Data Loading ✓
- Mock tool inventory from `tools.json`
- Test job specification from `test_integration_job.yaml`

### Phase 2: Resolution & Layout ✓
- **Resolution**: Flatten JobSpec → ResolvedLabel with cutter compensation
- **Verification Point 1**: Cutter selection (Label C should use 0.02" narrower cutter)
- **Layout**: Bin pack 204 label instances onto physical plates
- **Verification Point 2**: Multi-plate allocation (should generate 2 plates)

### Phase 3: Export ✓
- Vectorize each plate using vpype
- Export to PLT/HPGL format
- Generate 2 PLT files: `default_plate_1_raw.plt`, `default_plate_2_raw.plt`

### Phase 4: Visualization ✓ (Optional)
- Parse exported PLT files
- Generate PNG previews with cumulative distance coloring
- Output: `default_plate_1_raw_preview.png`, `default_plate_2_raw_preview.png`

## Test Labels

| Label | Type | Count | Purpose |
|-------|------|-------|---------|
| A | Explicit Override | 1 | Verify parameter override |
| B | Auto-Sized | 1 | Verify bounding-box calculation |
| C | Kerning Test | 1 | Verify 3x tolerance cutter logic |
| D | Volume Test | 200 | Verify multi-plate allocation |

## Verification Points

### ✓ Point 1: Cutter Selection
```
Label C (0.25" nominal):
  Ideal cutter: 0.03" (NOT available)
  Selected: 0.02" (narrower, dist=0.01)
  Toolpath: 0.23" (0.25 - 0.02)
  Status: ✓ PASS
```

### ✓ Point 2: Multi-Plate
```
Total instances: 204
Plate 1: ~97% utilized
Plate 2: ~51% utilized
Status: ✓ PASS (2 plates generated)
```

## Output Files

```
test_output/integration_test/
├── default_plate_1_raw.plt          # Raw toolpath (plate 1)
├── default_plate_1_raw_preview.png  # Visual preview (plate 1)
├── default_plate_2_raw.plt          # Raw toolpath (plate 2)
└── default_plate_2_raw_preview.png  # Visual preview (plate 2)
```

## Key Files

- `run_integration_test.py` - Test runner (4 phases, 2 verification points)
- `test_integration_job.yaml` - Test job spec (4 label types)
- `tools.json` - Mock cutter inventory
- `E2E_INTEGRATION_TEST_REPORT.md` - Full documentation

## Expected Results

### Console Output
```
✓ Pipeline executed successfully
```

### Verification Results
```
✓ PASS: Cutter selection logic correct
✓ PASS: Multi-plate allocation verified
```

### Generated Files
```
✓ Exported: test_output/integration_test/default_plate_1_raw.plt
✓ Exported: test_output/integration_test/default_plate_2_raw.plt
✓ Generated: test_output/integration_test/default_plate_1_raw_preview.png
✓ Generated: test_output/integration_test/default_plate_2_raw_preview.png
```

## Troubleshooting

**Problem**: Only 1 plate generated
- **Cause**: `test_integration_job.yaml` specifies constrained plates
- **Solution**: Remove `plates:` section to use unbounded mode

**Problem**: Cutter selection wrong
- **Cause**: `tools.json` doesn't match expected inventory
- **Solution**: Verify `tools.json` contains: [0.015, 0.02, 0.045, 0.125]

**Problem**: PNG visualization fails
- **Cause**: Missing matplotlib or parsing error
- **Solution**: This phase is optional; test passes regardless

## Next Steps

1. ✓ Run `python run_integration_test.py`
2. ✓ Inspect PNG previews in `test_output/integration_test/`
3. ✓ Verify cutter compensation on Label C (should be 0.23" height)
4. ✓ Verify multi-plate allocation (2 plates, good density)
5. ✓ Verify Label B auto-sizing produces correct dimensions
6. ✓ Verify text doesn't bleed over label boundaries

## Performance

- **Total test runtime**: ~5-10 seconds (including PNG generation)
- **Resolution time**: <1 ms
- **Bin packing time**: ~5 ms
- **Vectorization time**: ~50 ms
- **Visualization time**: ~2 seconds

---

**Status**: ✓ All phases passing  
**Last run**: 2026-07-19  
**Cutter inventory**: [0.015, 0.02, 0.045, 0.125]  
**Plates allocated**: 2 (unbounded mode)  
