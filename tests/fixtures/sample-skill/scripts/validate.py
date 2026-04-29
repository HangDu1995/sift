#!/usr/bin/env python3
"""Sample validation script for the test fixture skill."""

import json
import sys
from pathlib import Path


def validate(path: str) -> bool:
    p = Path(path)
    if not p.exists():
        print(f"File not found: {path}")
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON: {exc}")
        return False
    if not isinstance(data, list):
        print("Expected a JSON array")
        return False
    print(f"Valid: {len(data)} records")
    return True


if __name__ == "__main__":
    ok = validate(sys.argv[1]) if len(sys.argv) > 1 else False
    sys.exit(0 if ok else 1)
