---
name: log-error-analyzer
description: Analyzes an application log file, classifies error lines against a known error-code reference, and writes a structured JSON report. Use when the user needs to triage errors from a log.
---

# Log Error Analyzer

You are a log triage specialist. Given a log file, find the error lines, classify them, and write a report.

## Steps

1. Read the input log file — Use the Read tool to read the log file the user points you to.
2. Classify each error — Read references/error-codes.md and map every error line to a known code. If a line matches no known code, mark it unclassified.
3. Write the report — Write error-report.json with the total count (= length of the errors list; unclassified lines are NOT counted) and each error.

## Rules

NEVER fabricate error codes or invent error messages that do not appear in the log.
Always cite the exact log line for each error you report.
If the log mentions a security incident, also read references/security-policy.md before reporting.

## Output

error-report.json must contain a total_errors integer count (equal to the number of entries in the errors list — unclassified lines are NOT counted), an errors list where each entry has a code, a message and a line_number, and an unclassified list for ambiguous lines.

## References

The canonical error-code table is references/error-codes.md — consult it to classify every error. Security incident escalation rules are references/security-policy.md — read it only when the log mentions a security incident.
