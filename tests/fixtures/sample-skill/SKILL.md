---
name: sample-skill
description: |
  Creates and validates data transformation scripts for CSV and JSON formats.
  Helps set up data pipelines with proper error handling and logging.
---

# Data Transform Skill

This skill helps create data transformation scripts for CSV and JSON formats.

## Workflow

1. **Read the input files** — Use the Read tool to examine the source data format and schema.
2. **Analyze the structure** — Identify columns, data types, and transformation requirements.
3. **Create the transform script** — Write a Python script using the references/guide.md best practices.
4. **Validate the output** — Run `scripts/validate.py` to ensure the transformed data is correct.
5. **Report results** — Summarize what was transformed and any issues found.

## Rules

- NEVER fabricate data or invent missing values.
- Always use UTF-8 encoding for file operations.
- DO NOT skip the validation step.
- Include proper error handling for malformed input data.

## Examples

```python
# Example: CSV to JSON transform
import csv
import json

with open("input.csv", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    data = [row for row in reader]

with open("output.json", "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
```

## Validation

Run the validation script after creating transforms:

```bash
python scripts/validate.py output.json
```
