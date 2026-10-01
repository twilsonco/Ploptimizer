# Integration Guide: Auto-Generate Schema Docs from job-config.json

## Quick Summary

The `generate_schema_docs.py` script reads `job-config.json` and generates documentation of all available tool options. This keeps your schema documentation automatically synchronized with the actual tool options defined in your job config, without manual duplication.

**Two ways to use it:**
1. **Manual copy-paste** (for once-in-a-while updates)
2. **Pre-commit hook** (for automated sync on config changes)

---

## Workflow 1: Manual Documentation Updates

### Generate formatted documentation:
```bash
python3 docs/schema/generate_schema_docs.py --show
```

Output shows all 9 tool options with name, command, type, units, ranges, and defaults:
```
**Available Tool Options:**

- **cutting_velocity**: cutting velocity (VS, float, in/sec [0.05–3.0]). Defaults: text=0.8, borders_holes=0.8.
- **dwell_time**: dwell time (ZO124,, int, milliseconds [10–1000]). Defaults: text=50, borders_holes=50.
...
```

Use this for:
- ✅ Documentation websites (paste into README, docs folder, etc.)
- ✅ Help text in UIs
- ✅ API documentation generation
- ✅ User guides

### Generate Python code for schema.py docstring:
```bash
python3 docs/schema/generate_schema_docs.py --show-python-code
```

Output shows code ready to copy-paste into the `JobSpec.tool_options` field:
```python
            "\n"
            "**Available Tool Options:**\n"
            ""
            "- **cutting_velocity**: cutting velocity (VS, float, in/sec [0.05–3.0]). Defaults: text=0.8, borders_holes=0.8.\n"
            "- **dwell_time**: dwell time (ZO124,, int, milliseconds [10–1000]). Defaults: text=50, borders_holes=50.\n"
            ...
```

**To apply to schema.py:**
1. Run: `python3 docs/schema/generate_schema_docs.py --show-python-code`
2. Copy the output lines
3. Paste them at the end of the description string in `JobSpec.tool_options` field
4. Ensure proper indentation (should match surrounding lines)

---

## Workflow 2: Pre-Commit Hook Automation

Set up an automated pre-commit hook to update documentation whenever `job-config.json` changes.

### Step 1: Add to `.pre-commit-config.yaml`:

```yaml
# At the end of your existing .pre-commit-config.yaml:
  - repo: local
    hooks:
      - id: sync-schema-docs
        name: Sync schema docs from job-config
        entry: bash -c 'python3 docs/schema/generate_schema_docs.py --show-python-code > /tmp/schema_docs.txt && echo "Run: python3 docs/schema/generate_schema_docs.py --show-python-code and update JobSpec.tool_options"'
        language: system
        files: 'job-config\.json'
        stages: [commit]
        pass_filenames: false
```

Or simpler version (just a reminder):
```yaml
  - repo: local
    hooks:
      - id: remind-update-schema-docs
        name: Reminder to update schema docs
        entry: bash -c 'git diff --cached job-config.json > /dev/null && echo "⚠️  job-config.json changed - run: python3 docs/schema/generate_schema_docs.py --show-python-code"'
        language: system
        files: 'job-config\.json'
        stages: [commit]
        pass_filenames: false
        always_run: false
```

### Step 2: Enable the hook:
```bash
cd /path/to/PLT-Optimizer
pip install pre-commit  # if not already installed
pre-commit install
```

Now, any time you modify `job-config.json`, pre-commit will remind you to update the docs!

---

## Script Usage Summary

### Show all available options:
```bash
python3 docs/schema/generate_schema_docs.py --show
```

### Generate Python code for docstring:
```bash
python3 docs/schema/generate_schema_docs.py --show-python-code
```

### Specify non-default job-config path:
```bash
python3 docs/schema/generate_schema_docs.py --show --config /path/to/job-config.json
```

### Run with no arguments (shows both):
```bash
python3 docs/schema/generate_schema_docs.py
```

---

## What Gets Generated

Each tool option entry includes:
- **Key name**: Used in YAML specs (e.g., `cutting_velocity`)
- **Human name**: For UI/documentation (e.g., "cutting velocity")
- **HPGL command**: The actual command prefix (e.g., "VS")
- **Type**: `bool`, `int`, or `float`
- **Units**: Measurement unit (e.g., "in/sec", "milliseconds")
- **Range**: For numeric types, the min/max bounds (e.g., `[0.05–3.0]`)
- **Dual Defaults**: Separate defaults for text vs borders/holes layers

Example:
```
- **cutting_velocity**: cutting velocity (VS, float, in/sec [0.05–3.0]). 
  Defaults: text=0.8, borders_holes=0.8.
```

---

## Benefits

✅ **Single Source of Truth**: Tool options defined once in `job-config.json`  
✅ **Auto-Synced Docs**: Generate docs anytime the config changes  
✅ **Copy-Paste Ready**: Output is ready for schema.py or docs websites  
✅ **No Runtime Overhead**: Build-time generation, no cost at runtime  
✅ **Easy Maintenance**: Modify tool options → regenerate docs → commit  
✅ **Multi-Format**: Support both formatted text and Python docstring code  

---

## Example Workflow

**Scenario:** You add a new tool option to `job-config.json`.

1. Edit `job-config.json` and add the new option
2. Run: `python3 docs/schema/generate_schema_docs.py --show` to preview
3. Run: `python3 docs/schema/generate_schema_docs.py --show-python-code` to get code
4. Copy the new lines into `schema.py` `JobSpec.tool_options` docstring
5. Commit both `job-config.json` and `schema.py`

Done! Schema docs are now in sync with the actual tool options.

---

## Troubleshooting

**Script not found:**
```bash
cd /Users/haiiro/NoSync/PLT-Optimizer  # Navigate to repo root
python3 docs/schema/generate_schema_docs.py --show
```

**job-config.json not found:**
```bash
python3 docs/schema/generate_schema_docs.py --show --config ./job-config.json
```

**Pre-commit hook not running:**
```bash
pre-commit install  # Re-install hooks
pre-commit run remind-update-schema-docs --all-files  # Test manually
```

---

## Next Steps

1. **Try the script:**
   ```bash
   python3 docs/schema/generate_schema_docs.py --show
   ```

2. **(Optional) Set up pre-commit hook** for automatic reminders

3. **Update docs** whenever you modify `job-config.json` tool options
