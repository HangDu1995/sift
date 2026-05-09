# Schema Validation Rules

## Required Fields

| Field | Type | Constraints |
|-------|------|-------------|
| id | integer | Must be > 0 |
| name | string | Must not be empty |
| type | string | One of: user, order, product |

## Optional Fields

| Field | Type | Constraints |
|-------|------|-------------|
| email | string | Must contain @ |
| status | string | One of: active, inactive, pending |
