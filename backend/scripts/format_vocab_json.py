#!/usr/bin/env python3
"""Format shipped vocabulary JSON without changing entries or their order."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.vocabulary_json import readable_json

FILES = [*sorted((BACKEND / "data").glob("*.json")),
         *sorted((BACKEND / "scripts").glob("*.json"))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report unformatted files without writing")
    args = parser.parse_args()
    changed = 0
    for path in FILES:
        original = path.read_bytes()
        formatted = readable_json(json.loads(original)).encode("utf-8")
        if original == formatted:
            continue
        changed += 1
        if not args.check:
            path.write_bytes(formatted)
        print(f'{"Needs formatting" if args.check else "Formatted"}: {path.relative_to(BACKEND)}')
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
