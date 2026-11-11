"""Explicit, reversible priority normalization; never run implicitly on connection.

Snapshot BEFORE installing cap-aware imports (legacy wire values would be clipped).
Persist the returned manifest to an exclusive JSON file and attach it before apply.
Apply/undo fence every recorded value, refusing concurrent edits atomically. Undo
above the cap is a deliberate policy rollback, not an ordinary priority edit.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from hermes_cli.kanban_priority import PRIORITY_CAP


HEADROOM = 100


def _identity(conn) -> dict:
    from hermes_cli.kanban_db_sync import sync_state
    state = sync_state(conn) or {}
    return {key: state.get(key) for key in ("board_id", "gateway_id", "mode")}


def plan(conn) -> dict:
    """Map distinct positive open priorities monotonically into 1..900; keep ties.

    Nonpositive priorities retain their meaning. Refuse rather than collapse more
    distinct ranks than the target band can represent. Terminal rows are untouched.
    """
    rows = conn.execute("SELECT id, priority FROM tasks WHERE status NOT IN ('done','archived') ORDER BY id").fetchall()
    values = sorted({int(r["priority"] or 0) for r in rows if (r["priority"] or 0) > 0})
    ceiling = PRIORITY_CAP - HEADROOM
    if len(values) > ceiling:
        raise ValueError("too many distinct priorities for lossless normalization")
    ranks = {value: (index + 1) * ceiling // len(values) for index, value in enumerate(values)}
    return {"version": 1, "cap": PRIORITY_CAP, "identity": _identity(conn), "ceiling": ceiling, "rows": [
        {"id": r["id"], "old": int(r["priority"] or 0), "new": ranks.get(r["priority"], int(r["priority"] or 0))}
        for r in rows
    ]}


def save_manifest(path: Path, manifest: dict) -> None:
    """Durably record original values, refusing to overwrite an earlier backup."""
    with path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def apply(conn, path: Path, *, undo: bool = False, allow_legacy_rollback: bool = False) -> int:
    """Apply an already-saved manifest once, or explicitly roll back its values.

    Run on the board's authorized writer, not a worker replica. The normal edit
    transaction emits sync deltas; no sync flags or leases are bypassed. A second
    application is a no-op. Mixed old/new states or intervening changes refuse.
    """
    from hermes_cli import kanban_db as kb

    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("version") != 1 or manifest.get("cap") != PRIORITY_CAP:
        raise ValueError("unknown priority migration manifest")
    rows = manifest["rows"]
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("duplicate task in manifest")
    if any(not isinstance(r[k], int) for r in rows for k in ("old", "new")):
        raise ValueError("noninteger priority in manifest")
    if any(r["new"] > PRIORITY_CAP - HEADROOM for r in rows):
        raise ValueError("normalization must reserve headroom")
    if undo and any(r["old"] > PRIORITY_CAP for r in rows) and not allow_legacy_rollback:
        raise ValueError("restoring above-cap values requires explicit legacy policy rollback")
    source, target = ("new", "old") if undo else ("old", "new")
    changes = [r for r in rows if r[source] != r[target]]
    with kb.write_txn(conn):
        identity = _identity(conn)
        if identity != manifest.get("identity") or identity.get("mode") == "shadow":
            raise ValueError("manifest belongs to a different writer or a read-only shadow")
        if identity.get("mode") == "primary":
            foreign = conn.execute("SELECT id FROM tasks WHERE status NOT IN ('done','archived') "
                                   "AND created_on IS NOT NULL AND created_on != ? LIMIT 1",
                                   (identity["gateway_id"],)).fetchone()
            if foreign is not None:
                raise ValueError("foreign-owned rows require normalization on their origin writer")
        if not undo:
            live_ids = {r[0] for r in conn.execute("SELECT id FROM tasks WHERE status NOT IN ('done','archived')")}
            if live_ids - {r["id"] for r in rows}:
                raise ValueError("new open cards since snapshot; capture a new manifest")
        current = {r["id"]: conn.execute("SELECT priority FROM tasks WHERE id=?", (r["id"],)).fetchone()
                   for r in rows}
        if all(current[r["id"]] is not None and current[r["id"]][0] == r[target] for r in rows):
            return 0
        if any(current[r["id"]] is None or current[r["id"]][0] != r[source] for r in rows):
            raise ValueError("priorities changed since snapshot; capture a new manifest, do not overwrite")
        for row in changes:
            # This explicit rollback is the only path permitted to restore a legacy
            # value above the policy cap; regular edits always enforce the cap.
            conn.execute("UPDATE tasks SET priority=? WHERE id=?", (row[target], row["id"]))
            kb._append_event(conn, row["id"], "reprioritized", {
                "priority": row[target], "previous_priority": row[source],
                "reason": "priority normalization rollback" if undo else "priority normalization v1",
            })
    for row in changes:
        kb.notify_task_updated(conn, row["id"], ["priority"])
    return len(changes)
