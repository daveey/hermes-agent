"""Read back the origin chat's task projection, rather than replica link inventory."""
from __future__ import annotations

import json
import sqlite3
import sys


def lineage(sid: str) -> list[str]:
    from hermes_state import SessionDB
    from hermes_state_outcome import _lineage_ids

    db = SessionDB(read_only=True)
    try:
        return _lineage_ids(db, sid)
    finally:
        db.close()


def matches(row: dict, expected: str) -> bool:
    """Compare at the requested qualification; never discard a gateway or board."""
    key = str(row.get("key") or "")
    parts = key.split("/")
    if "/" not in expected:
        return expected == row.get("id")
    if expected.count("/") == 1:
        return expected == "/".join(parts[-2:])
    if expected.count("/") == 2 and len(parts) == 4:
        return expected == "/".join((parts[0], *parts[-2:]))
    if len(parts) == 2:
        from hermes_cli.kanban_origin import gateway_names
        key = f"{gateway_names()[0]}/{key}"
    return expected == key


def command(args) -> int:
    from hermes_cli import kanban_session_activity as activity

    try:
        rows = activity.session_tasks(lineage(args.session_id), activity.LISTED_STATUSES + ("done", "archived"))
        found = args.expect is None or any(matches(row, args.expect) for row in rows)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"kanban chat-tasks: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        for row in rows:
            print(f"{row.get('key', row['id'])}  {row.get('status', '')}  {row.get('title', '')}")
    return 0 if found else 4
