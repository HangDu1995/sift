# Code Review Patterns

Known bug patterns and style rules to check Python code against. Use these as a
checklist, but only report issues you can point to in the actual code.

## Bug patterns

- **Mutable default argument**: `def f(x=[])` or `def f(x={})` — the default object
  is shared and mutated across calls. Fix: use `None` and create the container inside.
- **Bare except**: `except:` with no exception type catches everything including
  `KeyboardInterrupt` and `SystemExit`. Catch specific exceptions instead.
- **Unclosed resource**: `open()`, sockets or database connections used without a
  `with` block or `try/finally`. Prefer context managers.
- **Variable shadowing**: a name in an inner scope hides an outer variable or a
  builtin (e.g. `list`, `id`, `type`, `dict`).
- **Off-by-one**: range bounds or slice edges that are off by one. Verify whether
  each bound is inclusive or exclusive.
- **Comparison to singleton**: `== None` / `== True` instead of `is None` / `is True`.

## Style rules

- A function that does too much (excessive length or mixing responsibilities).
- Magic numbers used inline without a named constant.
- Missing or misleading docstrings on public functions.
- Dead code or unreachable branches.
