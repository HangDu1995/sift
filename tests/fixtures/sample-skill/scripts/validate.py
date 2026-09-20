#!/usr/bin/env python3
"""Sample validation script for the test fixture skill."""

import json
import logging
import sys
from pathlib import Path


def validate(path: str) -> bool:
    p = Path(path)
    if not p.exists():
        _output(f"File not found: {path}", sys.stdout)
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _output(f"Invalid JSON: {exc}", sys.stdout)
        return False
    if not isinstance(data, list):
        _output("Expected a JSON array", sys.stdout)
        return False
    _output(f"Valid: {len(data)} records", sys.stdout)
    return True


def _output(message, stream):
    handler = logging.Handler()

    def emit(record):
        stream.write(record.getMessage() + "\n")

    handler.emit = emit
    handler.handle(logging.LogRecord(__name__, logging.INFO, __file__, 0, str(message), (), None))


if __name__ == "__main__":
    ok = validate(sys.argv[1]) if len(sys.argv) > 1 else False
    sys.exit(0 if ok else 1)
