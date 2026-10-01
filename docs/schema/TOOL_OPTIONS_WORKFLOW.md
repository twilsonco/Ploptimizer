# Tool Options Documentation Workflow

## Overview

Tool options are HPGL header commands that control engraver parameters (cutting velocity, spindle speed, etc.). They're documented in three places with complementary purposes:

| Where | What | For Whom |
|---|---|---|
| **`docs/schema/JOB_SPEC.md`** | Complete reference table | Users, AI agents, schema validation |
| **`docs/schema/generate_ai_docs.py`** | Auto-generation script | Developers (regenerate when config changes) |
| **`docs/schema/generate_schema_docs.py`** | Standalone utility | UI/documentation generation |

---

## The Complete Workflow

### For Users & AI Agents

**You want to use tool options in your job YAML:**

1. Read [`docs/schema/README.md`](README.md) for semantic overview
2. See [`docs/schema/JOB_SPEC.md`](JOB_SPEC.md) → "Available Tool Options" section for the complete table
3. Use in your YAML:

```yaml
job:
  job_name: "Custom Parameters"
  tool_options:
    cutting_velocity: 1.5      # in/sec [0.05–3.0]
    spindle_speed: 15000       # rpm [3000–18000]
    dwell_time: 75             # milliseconds [10–1000]
```

**That's it.** Everything you need is in the generated docs.

---

### For Developers (Keeping Docs in Sync)

**When you add a new tool option to `job-config.json`:**

1. Edit `job-config.json` and add the parameter with metadata:
   ```json
   "new_param": {
     "name": "new parameter",
     "command": "XX",
     "type": "float",
     "units": "some-unit",
     "default": {"text": 1.0, "borders_holes": 1.0},
     "min": 0.1,
     "max": 10.0
   }
   ```

2. Regenerate all schema docs:
   ```bash
   python3 docs/schema/generate_ai_docs.py
   ```
   This automatically:
   - Updates `JOB_SPEC.md` with the new tool option in the table
   - Regenerates JSON schemas
   - Preserves all hand-written semantics in `README.md`

3. Commit both `job-config.json` and the updated `docs/schema/*.md` / `*.json`:
   ```bash
   git add job-config.json docs/schema/
   git commit -m "feat: add new_param tool option"
   ```

4. **Drift detection:** If you forget to regenerate, `tests/test_job_spec_docs.py` will fail on CI.

---

## Three Scripts, Three Purposes

### 1. `docs/schema/generate_ai_docs.py` — Main Entry Point

**Purpose:** Generate complete schema documentation from Pydantic models + config

**When to use:**
- After modifying `plt_optimizer/generate/schema.py` or `job_config.py`
- After adding tool options to `job-config.json`
- Before committing schema changes

**Command:**
```bash
python3 docs/schema/generate_ai_docs.py
```

**Outputs:**
- `docs/schema/job_spec.schema.json` — JSON Schema for validation
- `docs/schema/job_config.schema.json` — JSON Schema for config validation
- `docs/schema/JOB_SPEC.md` — Complete markdown reference with tables + tool_options table

**Features:**
- Automatically includes tool_options table from live `job-config.json`
- Scrapes fallback constants from source files (always up-to-date)
- Deterministic output (no timestamps)
- Pinned by `tests/test_job_spec_docs.py` drift detection

---

### 2. `docs/schema/generate_schema_docs.py` — UI/Documentation Helper

**Purpose:** Format tool options for use in UIs, documentation sites, or other tools

**When to use:**
- Building a web UI that needs to display available tool options
- Generating API documentation
- Creating PDFs or other formats
- Need formatted list of options outside the main schema docs

**Commands:**
```bash
# Show nicely formatted list for documentation
python3 docs/schema/generate_schema_docs.py --show

# Generate Python code for copy-paste into docstrings
python3 docs/schema/generate_schema_docs.py --show-python-code

# Specify non-default config path
python3 docs/schema/generate_schema_docs.py --show --config /path/to/job-config.json
```

**Output:**
```
**Available Tool Options:**

- **cutting_velocity**: cutting velocity (VS, float, in/sec [0.05–3.0]). Defaults: text=0.8, borders_holes=0.8.
- **dwell_time**: dwell time (ZO124,, int, milliseconds [10–1000]). Defaults: text=50, borders_holes=50.
...
```

**Features:**
- Multiple output formats (human-readable, Python code)
- No file modifications (safe for scripts/CI)
- Can be piped into build systems
- Optional pre-commit hook integration

---

### 3. `JOB_SPEC.md` (Generated) — User-Facing Reference

**Purpose:** Complete schema reference for users and AI agents

**Contains:**
- JobSpec field table (all fields including `tool_options`)
- PlateSpec, LabelSpec, TextLine, HoleSpec tables
- Enum value tables
- Fallback defaults table
- Required-when-unconfigured list
- **Available Tool Options table** (all 9 parameters)

**How it's generated:**
- Source: `docs/schema/generate_ai_docs.py`
- Reads from: `schema.py`, `job_config.py`, `job-config.json`
- Generated on every run: fully deterministic

---

## Integration Points

### Pydantic Models → JSON Schema
```
plt_optimizer/generate/schema.py (JobSpec, PlateSpec, LabelSpec, ...)
                           ↓
                   model_json_schema()
                           ↓
                   job_spec.schema.json
```

### Config + Models → Markdown
```
job-config.json (tool_options metadata)
        ↓
plt_optimizer/generate/schema.py (JobSpec model)
        ↓
docs/schema/generate_ai_docs.py
        ↓
JOB_SPEC.md (with tool_options table)
```

### Config → Utility Formats
```
job-config.json (tool_options)
        ↓
docs/schema/generate_schema_docs.py
        ↓
human-readable markdown or Python code
```

---

## Complete Example: Adding a New Tool Option

**Step 1:** Edit `job-config.json`:
```json
{
  "tool_options": {
    "new_velocity": {
      "name": "new velocity",
      "command": "NV",
      "type": "float",
      "units": "in/sec",
      "default": {"text": 2.5, "borders_holes": 2.5},
      "min": 0.5,
      "max": 5.0
    }
  }
}
```

**Step 2:** Regenerate docs:
```bash
python3 docs/schema/generate_ai_docs.py
```

**Step 3:** Check the result in `docs/schema/JOB_SPEC.md`:
```
| `new_velocity` | `NV` | float | in/sec [0.5–5.0] | 2.5 | 2.5 |
```

**Step 4:** Commit:
```bash
git add job-config.json docs/schema/
git commit -m "feat: add new_velocity tool option"
```

**Done!** Users can now see it in the reference and use it in their YAML.

---

## Testing & Drift Detection

- `tests/test_job_spec_docs.py` verifies that generated artifacts are in sync with models
- If you modify `schema.py` or `job_config.py` without regenerating, the test fails
- This ensures the reference documentation never becomes stale
- Run before committing:
  ```bash
  python3 -m pytest tests/test_job_spec_docs.py -v
  ```

---

## FAQ

**Q: Do I need to run both `generate_ai_docs.py` and `generate_schema_docs.py`?**  
A: No. `generate_ai_docs.py` is the main workflow. `generate_schema_docs.py` is only for special use cases (UIs, alternate formats).

**Q: When should I regenerate?**  
A: After any change to `schema.py`, `job_config.py`, or `job-config.json`.

**Q: What if I forget to regenerate?**  
A: The CI test `tests/test_job_spec_docs.py` will catch it.

**Q: Can I manually edit `JOB_SPEC.md`?**  
A: No, it's auto-generated. Edit the source files (`schema.py`, `job_config.py`, `job-config.json`) and regenerate.

**Q: How do users stay informed of new tool options?**  
A: They reference `docs/schema/JOB_SPEC.md` which is always up-to-date.

---

## Summary

| Task | Script | Command |
|---|---|---|
| **Generate all schema docs** | `docs/schema/generate_ai_docs.py` | `python3 docs/schema/generate_ai_docs.py` |
| **Format options for UI** | `docs/schema/generate_schema_docs.py` | `python3 docs/schema/generate_schema_docs.py --show` |
| **Validate docs are current** | pytest | `python3 -m pytest tests/test_job_spec_docs.py` |
