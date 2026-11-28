"""Owner-qualified SessionState dependency reads from live and Board shadow snapshots. [daveey]

Local membership owns its entire edge set, including an empty set after unlink.
Foreign roots must match the replica owner; no same-ID local row can stand in for
one. Unknown parents remain indexed placeholders so a later arrival can wake them.
"""
from __future__ import annotations

import math
import sqlite3
import time
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any

from hermes_session_state_collect import MAX_CLOSURE, MAX_DEPTH, _parents, _rows, block_episodes, is_remote


def _snapshot(stack: ExitStack, path: Path):
    from hermes_cli.kanban_generation import open_ro

    conn = stack.enter_context(closing(open_ro(path, timeout=0.5)))
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    return conn


def _freshness(conn, now: float) -> dict:
    from hermes_cli.kanban_session_activity import HUB_STALE_AFTER_S

    try:
        row = conn.execute("SELECT pulled_at FROM kanban_sync_state LIMIT 1").fetchone()
        stamp = row[0] if row else None
    except sqlite3.Error:
        stamp = None
    valid = isinstance(stamp, (float, int)) and not isinstance(stamp, bool) and math.isfinite(stamp) and 0 < stamp <= now
    stale = not valid or now - stamp > HUB_STALE_AFTER_S
    return {"stale": stale, "synced_at": stamp if valid else None,
            "source_expires_at": stamp + HUB_STALE_AFTER_S + 0.001 if valid and not stale else None}


def _read(conn, tid: str) -> dict | None:
    if conn is None:
        return None
    rows = _rows(conn, {tid})
    row = rows.get(tid)
    if row is not None:
        owner = conn.execute("SELECT created_on FROM tasks WHERE id=?", (tid,)).fetchone()
        row["gateway"] = str(owner[0] or "?")
    return row


def collect_graph(cards: list[dict], paths: dict[str, Path], gateway: str) -> dict[str, Any]:
    """Read bounded closure with owner-qualified keys and explicit partial coverage."""
    from hermes_cli.kanban_sync_client import shadow_path

    result: dict[str, Any] = {"rows": {}, "parents": {}, "episodes": {}, "errors": {}}
    for board in sorted({str(c.get("board") or "default") for c in cards}):
        roots = [c for c in cards if str(c.get("board") or "default") == board]
        with ExitStack() as stack:
            live = replica = None
            try:
                if board in paths:
                    live = _snapshot(stack, paths[board])
                path = shadow_path(board)
                if path.is_file():
                    replica = _snapshot(stack, path)
                _board(result, roots, board, gateway, live, replica)
            except (sqlite3.Error, OSError) as exc:
                result["errors"][f"closure:{board}"] = f"closure_read_failed:{type(exc).__name__}"
                for root in roots:
                    key = str(root.get("key") or f"{board}/{root['id']}")
                    result["rows"].setdefault(key, {**root, "linked": True,
                                                   "partial_source_unknown": "closure_read_failed"})
                    result["parents"].setdefault(key, [])
                for row in result["rows"].values():
                    if str(row.get("board") or "default") == board:
                        row["partial_source_unknown"] = "closure_read_failed"
    return result


def _board(out: dict, roots: list[dict], board: str, gateway: str, live, replica) -> None:
    from hermes_cli.kanban_triage_attention import annotate

    fresh = _freshness(replica, time.time()) if replica is not None else {"stale": True, "synced_at": None}
    listed = {str(c.get("key") or f"{board}/{c['id']}"): c for c in roots}
    queue = [(key, c, 0, True) for key, c in listed.items()]
    visited: set[str] = set()
    while queue:
        key, row, depth, linked = queue.pop(0)
        if key in visited:
            continue
        if not linked and sum(not r["linked"] for r in out["rows"].values()) >= MAX_CLOSURE:
            out["errors"][f"closure:{board}"] = "closure_truncated"
            continue
        visited.add(key)
        tid = str(row["id"])
        remote = is_remote(row)
        conn = replica if remote else live
        # Board shadows carry default-profile cards. A named-profile ledger row
        # is a different owner, not permission to borrow a matching default ID.
        profile = row.get("profile") or row.get("owner_profile") or "default"
        raw = _read(replica, tid) if remote else (_rows(live, {tid}).get(tid) if live is not None else None)
        matched = raw is not None and (not remote or (raw["gateway"] != "?"
                  and raw["gateway"] == row.get("gateway") and profile == "default"))
        evidence = {**row, "board": board, "key": key, "linked": linked}
        if remote:
            evidence.update(fresh)
            evidence["stale"] = bool(fresh["stale"] or row.get("stale"))
        if not matched:
            evidence["partial_source_unknown"] = "dependency_source_unavailable"
            out["errors"][f"closure:{key}"] = "dependency_source_unavailable"
            out["rows"][key] = evidence
            out["parents"][key] = []
            continue
        annotate(conn, [evidence])
        out["rows"][key] = evidence
        episode = block_episodes(conn, {tid}).get(tid)
        if episode is not None:
            out["episodes"][key] = episode
        pids = sorted(p for p, _child in _parents(conn, {tid}))
        if pids and depth >= MAX_DEPTH:
            out["errors"][f"closure:{key}"] = "closure_truncated"
            out["parents"][key] = []
            continue
        parent_keys = []
        for pid in pids:
            # Live membership is authoritative only within the local graph. A
            # foreign root's dependencies are resolved within its Board shadow.
            local = _rows(live, {pid}).get(pid) if not remote and live is not None else None
            parent = local or _read(replica, pid)
            if local is not None:
                pkey = f"{board}/{pid}"
            else:
                parent = parent or {"id": pid, "status": "unknown", "gateway": "?"}
                pkey = f"{parent['gateway']}/default/{board}/{pid}"
                parent.update(fresh)
            parent_keys.append(pkey)
            queue.append((pkey, listed.get(pkey, parent), depth + 1, pkey in listed))
        out["parents"][key] = parent_keys
