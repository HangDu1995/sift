# Error Code Reference

Canonical error codes for log triage. Map every error line to one of these codes. If a line matches none of them, mark it `unclassified` — do NOT invent a code.

| Code | Severity | Meaning |
|------|----------|---------|
| E-AUTH-001 | high | Authentication failed — token invalid or expired |
| E-DB-002 | high | Database connection lost |
| E-NET-003 | medium | Network request timed out |
| E-CONFIG-004 | low | Invalid configuration value |

Any code not in this table is fabricated and must not appear in the report.
