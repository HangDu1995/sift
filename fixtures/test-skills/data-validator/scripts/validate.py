"""Quick validation script for JSON data."""
import json
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        print("Usage: python validate.py <file.json>")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"FAIL: File not found: {path}")
        sys.exit(1)

    data = json.loads(path.read_text())
    if isinstance(data, dict):
        data = [data]

    errors = []
    for i, record in enumerate(data):
        if "id" not in record:
            errors.append(f"Record {i}: missing 'id'")
        if not record.get("name"):
            errors.append(f"Record {i}: missing or empty 'name'")
        if record.get("type") not in ("user", "order", "product"):
            errors.append(f"Record {i}: invalid 'type'")

    if errors:
        for e in errors:
            print(f"FAIL: {e}")
        sys.exit(1)
    print(f"PASS: {len(data)} records validated")


if __name__ == "__main__":
    main()
