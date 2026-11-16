"""Move-scoped mirror of dependency results; never an exemption from PR policy.

The primary file owns only this gateway's cards. Moving a parked owner must not
lose the parent results the ordinary dependency/review checks need. Refresh these
from the Board replica together with foreign parent statuses, not from move prose.
"""
from __future__ import annotations

import json

_TABLE = "kanban_move_parent_results"


def _parked_review_checkpoint(conn, task_id, run):
    """The authenticated review checkpoint of ``run``'s own latest park, or None.

    Only ``block_task(expected_run_id=...)`` stamps it. It must name ``run`` by
    portable identity (the importing file's integer run id differs).
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_dependency_review import _checkpoint_matches_run

    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'dependency_wait' AND run_id = ? "
        "ORDER BY id DESC LIMIT 1", (task_id, run["id"]),
    ).fetchone()
    payload = kb._json_dict(row[0]) if row else {}
    evidence = payload.get("review_checkpoint")
    if (payload.get("kind") != "dependency" or not isinstance(evidence, dict)
            or not _checkpoint_matches_run(evidence, run)):
        return None
    parents = evidence.get("parents")
    if not isinstance(parents, list) or not all(isinstance(p, str) for p in parents):
        return None
    return evidence


def _parked_scope(live):
    """Incremental memo (``kanban_move_parent_scope``); the uncached query is ``_parked_scope_uncached``."""
    from hermes_cli import kanban_move_parent_scope as scope

    return scope.parked_scope(live)


def _parked_scope_uncached(live):
    """Foreign parents an orderly owner park named in its review checkpoint. [daveey]

    A worker that parks on a review it created on its own gateway leaves that
    review foreign to the card's home file; without this the home guard could
    never read the verdict (t_01a1cde626be2ada, t_01a1c59e0410b2fb).
    """
    from hermes_cli.kanban_unused_grant import latest_started_run

    scoped = set()
    for (task_id,) in live.execute(
        # Only children of a foreign parent can need the mirror.
        "SELECT DISTINCT t.id FROM tasks t JOIN task_links l ON l.child_id = t.id "
        "LEFT JOIN tasks p ON p.id = l.parent_id "
        "WHERE p.id IS NULL AND t.status NOT IN ('done', 'archived')"
    ):
        run = latest_started_run(live, task_id)
        if run is None or run["outcome"] != "blocked":
            continue
        evidence = _parked_review_checkpoint(live, task_id, run)
        if evidence:
            scoped.update(evidence["parents"])
    return scoped


_QUEUED = (
    "SELECT e.task_id, e.payload FROM tasks t JOIN task_events e ON e.id = "
    "(SELECT MAX(m.id) FROM task_events m WHERE m.task_id = t.id AND m.kind = 'moved') "
    "WHERE t.status NOT IN ('done', 'archived')"
)


def _queued_checkpoint(payload):
    from hermes_cli import kanban_db as kb

    payload = kb._json_dict(payload)
    checkpoint = payload.get("checkpoint")
    return payload.get("queued") is True and isinstance(checkpoint, str) and bool(checkpoint.strip())


def _queued_scope_uncached(live):
    from hermes_cli import kanban_db as kb

    scoped = set()
    for event in live.execute(_QUEUED):
        if _queued_checkpoint(event["payload"]):
            scoped.update(kb.parent_ids(live, event["task_id"]))
    return scoped


def _evidence_scope_uncached(live):
    return _parked_scope_uncached(live) | _queued_scope_uncached(live)


def _evidence_scope(live):
    from hermes_cli import kanban_move_parent_scope as scope

    return scope.evidence_scope(live)


def payload(replica, parent):
    """The mirrored result of ``parent`` (its latest replica run), or None when the replica has none. Bookkeeping
    run columns (claims, heartbeats, sync version) are left out: no reader uses them and the P2 cursor never marks
    them, so keeping them would make the mirror differ from the keyed refresh until the next floor."""
    from hermes_cli.kanban_activity_revision import BOOKKEEPING_COLUMNS

    row = replica.execute(
        "SELECT r.*, t.status AS parent_status, EXISTS (SELECT 1 FROM task_events e "
        "WHERE e.task_id = r.task_id AND e.run_id = r.id AND e.kind = 'claimed') AS was_claimed "
        "FROM task_runs r JOIN tasks t ON t.id = r.task_id WHERE t.id = ? "
        "ORDER BY r.id DESC LIMIT 1", (parent,),
    ).fetchone()
    if row is None:
        return None
    skip = BOOKKEEPING_COLUMNS["task_runs"]
    return json.dumps({k: row[k] for k in row.keys() if k not in skip})


def stored(live, parents):
    """Current mirrored payloads of ``parents`` (absent when the table or the row is missing)."""
    if not live.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_TABLE,)).fetchone():
        return {}
    out = {}
    parents = list(parents)
    for i in range(0, len(parents), 500):
        chunk = parents[i:i + 500]
        out.update(live.execute(
            f"SELECT parent_id, payload FROM {_TABLE} WHERE parent_id IN ({','.join('?' * len(chunk))})", chunk))
    return out


def apply_rows(live, rows):
    """Row-keyed ``apply``: ``rows`` maps parent -> payload, None deletes. Caller owns the transaction."""
    live.execute(f"CREATE TABLE IF NOT EXISTS {_TABLE} (parent_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    live.executemany(f"DELETE FROM {_TABLE} WHERE parent_id = ?", [(p,) for p in rows])
    live.executemany(f"INSERT INTO {_TABLE} VALUES (?, ?)", [(p, v) for p, v in rows.items() if v is not None])


def prepare(live, replica, wanted, scoped=None):
    # Only explicit queued-move checkpoints and authenticated owner parks scope evidence.
    scoped = _evidence_scope(live) if scoped is None else scoped
    values = []
    for parent in sorted(set(wanted) & scoped):
        value = payload(replica, parent)
        if value is not None:
            values.append((parent, value))
    exists = live.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_TABLE,)).fetchone()
    current = dict(live.execute(f"SELECT parent_id, payload FROM {_TABLE}")) if exists else {}
    updated = dict(values)
    changed = sorted(k for k in set(current) | set(updated) if current.get(k) != updated.get(k))
    return values, changed


def apply(live, values):
    """Caller owns the transaction shared with the parent-status refresh."""
    live.execute(f"CREATE TABLE IF NOT EXISTS {_TABLE} (parent_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    live.execute(f"DELETE FROM {_TABLE}")
    live.executemany(f"INSERT INTO {_TABLE} VALUES (?, ?)", values)


def _checkpointed_move_after(conn, task_id, ended):
    from hermes_cli import kanban_db as kb

    move = conn.execute(
        "SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind = 'moved' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if move is None:
        return False
    if not _queued_checkpoint(move["payload"]):
        return False
    moved = kb._to_epoch(move["created_at"])
    return moved is not None and moved >= ended


def owner_faulted_since(conn, task_id, since) -> bool:
    """True when the owner crashed, was reclaimed or timed out at/after ``since``.

    A park authorizes one continuation of an orderly owner only; a fault after it
    re-arms the hold whichever foreign source (this mirror or the Board replica)
    supplies the parent's result.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_core_update_requeue import requeued_run_ids

    paused = requeued_run_ids(conn, task_id)
    faults = conn.execute(
        "SELECT created_at, run_id FROM task_events WHERE task_id = ? "
        "AND kind IN ('crashed', 'reclaimed', 'timed_out')", (task_id,),
    )
    return any((kb._to_epoch(e["created_at"]) or 0) >= since and e["run_id"] not in paused
               for e in faults)


def result(conn, task_id, parent_id):
    """Read only for an orderly parked owner: after a checkpointed queued move, or
    when the owner's own authenticated review park names this parent."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_unused_grant import latest_started_run

    if kb.get_task(conn, parent_id) is not None:
        return None  # A local parent's own rows always decide; no stale mirror fallback.
    if parent_id not in dict(kb.parent_states(conn, task_id)):
        return None
    run = latest_started_run(conn, task_id)
    if run is None or run["outcome"] != "blocked":
        return None
    ended = kb._to_epoch(run["ended_at"])
    if ended is None:
        return None
    if not _checkpointed_move_after(conn, task_id, ended):
        evidence = _parked_review_checkpoint(conn, task_id, run)
        if evidence is None or parent_id not in evidence["parents"]:
            return None
    if owner_faulted_since(conn, task_id, ended):
        return None
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (_TABLE,)).fetchone():
        return None
    row = conn.execute(f"SELECT payload FROM {_TABLE} WHERE parent_id = ?", (parent_id,)).fetchone()
    return kb._json_dict(row[0]) if row else None
