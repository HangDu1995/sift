# Data Transformation Guide

## Best Practices

1. Always validate input data before transformation.
2. Use streaming for large files (>100MB).
3. Log transformation statistics (rows read, written, skipped).
4. Handle encoding explicitly — default to UTF-8.

## Error Handling

- Catch `UnicodeDecodeError` and try alternative encodings.
- Log malformed rows with line numbers.
- Continue processing after non-fatal errors.
