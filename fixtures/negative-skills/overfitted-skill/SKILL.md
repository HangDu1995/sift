---
name: overfitted-skill
description: A data processing skill that transforms JSON files according to specifications. Use when user needs to handle data files or process records for reporting purposes or generate reports from data sources.
---

# Data Processing Skill

## Process Data

Read the input file. Process all records. Write output.

## Format Rules

1. Output MUST include a `metadata` field containing `transform_type`, `source`, `record_count`, and `transformed_at`
2. Output MUST wrap records in a `records` array under the root object
3. The `record_count` MUST match exactly the number of records in the input
4. Filtered output MUST be written to `filtered_output.json`
5. Flattened output MUST be written to `flattened_output.json`
6. NEVER use `List` type — always use array literal syntax
7. ALWAYS validate that `id` fields are positive integers
8. ALWAYS validate that `name` fields are non-empty strings
9. ALWAYS check for the `type` field and ensure it is one of: user, order, product
10. ALWAYS include anti-SQL-injection validation
11. Report generation MUST produce both `report.md` and `report-summary.json`
12. All output files MUST be encoded in UTF-8 without BOM
