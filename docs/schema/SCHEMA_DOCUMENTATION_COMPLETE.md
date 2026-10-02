# ✅ Schema Documentation Integration - COMPLETE

## Summary

You now have a **clean, auto-generated schema documentation system** that keeps tool options and all other configuration in sync with the actual code. Users and AI agents can reference complete documentation, and developers have a simple workflow to maintain it.

## What You Get

### For Users & AI Agents 🎯

**Three reference documents** (all auto-generated, always in sync):

1. **[`docs/schema/README.md`](docs/schema/README.md)** — Semantic guide
   - Explains job forms, cascading attributes, replacement files, etc.
   - Walk through conceptually before diving into reference docs

2. **[`docs/schema/JOB_SPEC.md`](docs/schema/JOB_SPEC.md)** — Complete field reference
   - JobSpec, PlateSpec, LabelSpec, TextLine, HoleSpec tables
   - Enum values
   - Fallback defaults
   - **NEW:** "Available Tool Options" table (all 9 parameters with commands, types, units, ranges, defaults)

3. **[`docs/schema/job_spec.schema.json`](docs/schema/job_spec.schema.json)** — Machine-readable schema
   - JSON Schema for validation
   - IDE integration (autocomplete, linting in VS Code, PyCharm, etc.)

### Example: Checking Tool Options

```bash
# Read the semantic overview
cat docs/schema/README.md | grep -A 10 "## Tool Options"

# See all available parameters
grep "## Available Tool Options" docs/schema/JOB_SPEC.md -A 20

# Machine validate against schema
python3 -c "
import json
from jsonschema import validate
spec = json.load(open('docs/schema/job_spec.schema.json'))
job = {'job': {'job_name': 'test', 'tool_options': {'cutting_velocity': 1.5}}}
validate(job, spec)  # raises if invalid
"
```

---

## For Developers 👨‍💻

**Simple three-step workflow** (when you modify config or models):

### Step 1: Make your change
```bash
# Edit one of these:
# - job-config.json (add/modify tool option)
# - plt_optimizer/generate/schema.py (add/modify JobSpec field)
# - plt_optimizer/generate/job_config.py (add/modify JobDefaults field)
vim job-config.json
```

### Step 2: Regenerate docs
```bash
python3 docs/schema/generate_ai_docs.py
# Output:
#   wrote docs/schema/job_spec.schema.json (XXXX lines)
#   wrote docs/schema/job_config.schema.json (XXXX lines)
#   wrote docs/schema/JOB_SPEC.md (XXXX lines)
```

### Step 3: Commit
```bash
git add job-config.json docs/schema/
git commit -m "feat: add new_tool_option to job-config.json"
```

**That's it.** Drift detection (`tests/test_job_spec_docs.py`) ensures you don't forget.

---

## The Three Documentation Scripts

| Script | Purpose | When to Use | Command |
|---|---|---|---|
| **`docs/schema/generate_ai_docs.py`** | Generate all schema docs | After editing config/models (main entry point) | `python3 docs/schema/generate_ai_docs.py` |
| **`docs/schema/generate_schema_docs.py`** | Format tool_options for UIs | Building a web UI, alternate documentation | `python3 docs/schema/generate_schema_docs.py --show` |
| **`tests/test_job_spec_docs.py`** | Detect drift | Before committing (runs in CI) | `python3 -m pytest tests/test_job_spec_docs.py` |

---

## Integration Flow (Data Architecture)

```
job-config.json (HPGL commands, types, units, ranges)
    ↓
JobSpec model (plt_optimizer/generate/schema.py)
    ↓
generate_ai_docs.py (_load_tool_options + _format_tool_options_markdown)
    ↓
JOB_SPEC.md (tool_options table in "Available Tool Options" section)
    ↓
Users reference docs/schema/README.md + JOB_SPEC.md + JSON Schema
```

Each step is automatic and deterministic.

---

## Example: Adding a New Tool Option

**Scenario:** You want to add a new engraver parameter `reverse_feed_rate`.

### Step 1: Edit `job-config.json`
```json
{
  "tool_options": {
    ...existing options...,
    "reverse_feed_rate": {
      "name": "reverse feed rate",
      "command": "RF",
      "type": "float",
      "units": "in/sec",
      "default": {"text": 1.0, "borders_holes": 1.0},
      "min": 0.1,
      "max": 3.0
    }
  }
}
```

### Step 2: Regenerate
```bash
python3 docs/schema/generate_ai_docs.py
```

### Step 3: Check the result
```bash
grep -A 15 "## Available Tool Options" docs/schema/JOB_SPEC.md
```
You'll see:
```
| `reverse_feed_rate` | `RF` | float | in/sec [0.1–3.0] | 1.0 | 1.0 |
```

### Step 4: Users can now use it
```yaml
job:
  job_name: "Fast Labels"
  tool_options:
    reverse_feed_rate: 2.5
```

### Step 5: Commit
```bash
git add job-config.json docs/schema/
git commit -m "feat: add reverse_feed_rate tool option"
```

**Done!** The complete reference is automatically updated.

---

## Verification

### All tests pass
```bash
python3 -m pytest tests/test_tool_options.py -v   # 18/18 ✅
python3 -m pytest tests/test_job_spec_docs.py -v  # 13/14 ✅ (1 skipped vpype)
```

### Generated docs are complete
```bash
# Tool options section exists
grep -c "## Available Tool Options" docs/schema/JOB_SPEC.md  # 1 ✅

# All 9 tool options are listed
grep "| \`" docs/schema/JOB_SPEC.md | grep -c "in/sec\|milliseconds\|rpm\|on/off" # 9 ✅

# Schema files regenerated
ls -lh docs/schema/*.json  # Both exist and are recent ✅
```

---

## Key Insights Resolved

### Original Questions

**Q1: "Let's add a 'units' key to each tool options object"**
- ✅ Done. job-config.json has units field extracted from PLT reference files
- All 9 tool options have units documented
- Units appear in generated `JOB_SPEC.md` table

**Q2: "How does `generate_schema_docs.py` get called? Concerned about keeping docs clean for users/AI agents"**
- ✅ Resolved. `generate_ai_docs.py` is the main entry point (not `generate_schema_docs.py`)
- `generate_schema_docs.py` is a helper utility for alternate formats (UIs, etc.)
- Users reference auto-generated `JOB_SPEC.md` + `README.md` → always clean, always in sync
- Drift detection prevents staleness

---

## Reference Documentation

For the complete workflow explanation and FAQ:
→ **[`docs/schema/TOOL_OPTIONS_WORKFLOW.md`](docs/schema/TOOL_OPTIONS_WORKFLOW.md)**

This document explains:
- Purpose of each script
- Complete integration points
- Example workflows
- Testing strategy
- Common questions (FAQ)

---

## Quick Command Reference

```bash
# For Users:
cat docs/schema/JOB_SPEC.md | grep -A 15 "Available Tool Options"

# For Developers:
python3 docs/schema/generate_ai_docs.py    # Regenerate all docs
python3 docs/schema/generate_schema_docs.py --show  # Show tool options nicely formatted

# For Testing:
python3 -m pytest tests/test_job_spec_docs.py  # Drift detection

# For CI:
python3 -m pytest tests/test_tool_options.py    # All tool option features
python3 -m pytest tests/test_job_spec_docs.py   # Docs in sync
```

---

## Summary

| What | Where | How to Update |
|---|---|---|
| User reference (for writing YAML) | docs/schema/README.md + JOB_SPEC.md | Auto-generated, run `python3 docs/schema/generate_ai_docs.py` |
| JSON Schema (for validation) | docs/schema/job_spec.schema.json | Auto-generated, run `python3 docs/schema/generate_ai_docs.py` |
| Tool options source | job-config.json | Edit directly, then regenerate docs |
| Workflow documentation | docs/schema/TOOL_OPTIONS_WORKFLOW.md | Reference guide (hand-written but permanent) |

**Result:** Clean, self-documenting system where users always see up-to-date reference docs, and developers have a simple maintenance workflow.

---

## Commits

```
ac00f8e - docs: add comprehensive tool_options workflow guide
a9f1fdc - docs: integrate tool_options into auto-generated schema documentation
```

These commits:
1. ✅ Integrate tool_options into main schema doc generation system
2. ✅ Add helper functions to read and format tool_options from job-config.json
3. ✅ Update README.md with tool_options explanation
4. ✅ Create comprehensive workflow guide for developers
5. ✅ Pass all tests and pre-commit checks
6. ✅ Provide FAQ and integration architecture docs

---

## Next Steps (Optional Enhancements)

These are future work, not blockers:
- [ ] Web UI to visualize/edit tool options
- [ ] IDE/LSP integration for YAML autocomplete
- [ ] Pre-commit hook to auto-regenerate docs on config change
- [ ] CLI subcommand: `plt-optimizer schema info tool-options`
- [ ] OpenAPI spec generation for REST API

The current system is production-ready and maintainable.
