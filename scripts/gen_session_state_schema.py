#!/usr/bin/env python3
"""Regenerate ``schemas/session_state_v2.schema.json`` from ``hermes_session_state_schema.JSON_SCHEMA``. [daveey]

``--check`` exits 1 when the committed file differs (the schema test runs the same comparison)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "schemas" / "session_state_v2.schema.json"


def render() -> str:
    sys.path.insert(0, str(ROOT))
    from hermes_session_state_schema import JSON_SCHEMA

    return json.dumps(JSON_SCHEMA, indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        return 0 if TARGET.is_file() and TARGET.read_text(encoding="utf-8-sig") == text else 1
    TARGET.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
