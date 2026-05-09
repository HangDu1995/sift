# Report Template Guidelines

## Markdown Report Structure

1. Title header
2. Summary section with counts
3. Data Quality section
4. Recommendations section

## Quality Score Calculation

- Start at 1.0
- -0.1 per record with missing optional fields
- -0.2 per record with missing required fields
- Clamp to [0.0, 1.0]
