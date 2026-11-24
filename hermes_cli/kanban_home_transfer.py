"""Dormant local preparation for a cooperative administrative-home transfer.

This is NOT a Board ACK, migration command or permission to change created_on.
No production caller prepares a transfer yet. It is one half of a coordinated
protocol: fences remain durable on an unknown outcome, with no timed release.
SQLite cannot revoke already serialized packets; Board ingress fencing is also
required before this substrate can be used for a production transfer. [daveey]
"""
from __future__ import annotations

import re
import sqlite3

from hermes_cli import kanban_home_abort as abort

_SOURCE = re.compile(r"worker-[a-z0-9-]{1,24}\Z")
_NONCE = re.compile(r"[A-Za-z0-9._:-]{16,128}\Z")
_TABLE = "kanban_home_prepared1"


def _install(conn: sqlite3.Connection) -> None:
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
        task_id TEXT PRIMARY KEY, source TEXT NOT NULL, destination TEXT NOT NULL,
        nonce TEXT NOT NULL, expected_version INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS kanban_home_finalized1 (
        task_id TEXT PRIMARY KEY, nonce TEXT NOT NULL, receipt TEXT NOT NULL,
        CHECK(json_valid(receipt) AND json_extract(receipt,'$.task') IS task_id
            AND json_extract(receipt,'$.nonce') IS nonce
            AND json_extract(receipt,'$.phase') IS 'materialized'
            AND json_type(receipt,'$.rows') IS 'array'))""")
    for action in ("UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS home_finalized1_{action.lower()}
            BEFORE {action} ON kanban_home_finalized1
            BEGIN SELECT RAISE(ABORT, 'home finalization immutable'); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS home_finalized1_insert
        BEFORE INSERT ON kanban_home_finalized1
        WHEN EXISTS(SELECT 1 FROM kanban_home_finalized1 WHERE task_id=NEW.task_id)
        BEGIN SELECT RAISE(ABORT, 'home finalization immutable'); END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS home_finalized1_scope
        BEFORE INSERT ON kanban_home_finalized1
        WHEN NOT EXISTS(SELECT 1 FROM {_TABLE} p WHERE p.task_id=NEW.task_id AND p.nonce=NEW.nonce
            AND p.source IS json_extract(NEW.receipt,'$.source')
            AND p.destination IS json_extract(NEW.receipt,'$.destination')
            AND p.expected_version IS json_extract(NEW.receipt,'$.expected_version')
            AND json_type(NEW.receipt,'$.expected_version') IS 'integer')
        BEGIN SELECT RAISE(ABORT, 'home finalization scope mismatch'); END""")
    abort.install(conn)
    # Include local-only task custody (wake/ask probes, subscriptions, private
    # restriction records), not just the rows sent by sync. Discover task_id
    # tables from this board's real schema, retaining guards across sync detach.
    related = {}
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        quoted = '"' + table.replace('"', '""') + '"'
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({quoted})")}
        if "task_id" in columns and table not in (_TABLE, "kanban_home_finalized1", "kanban_home_aborted1"):
            related[table] = columns
    for table in ("tasks", "task_links", *sorted(related)):
        quoted = '"' + table.replace('"', '""') + '"'
        for action in ("INSERT", "UPDATE", "DELETE"):
            refs = ["NEW"] if action == "INSERT" else ["OLD"] if action == "DELETE" else ["OLD", "NEW"]
            if table == "tasks":
                ids = [f"{ref}.id" for ref in refs]
            elif table == "task_links":
                ids = [f"{ref}.{col}" for ref in refs for col in ("parent_id", "child_id")]
            else:
                ids = [f"{ref}.task_id" for ref in refs]
            tests = [f"task_id IN ({','.join(ids)})"]
            if action == "INSERT":
                # REPLACE's implicit deletion need not fire DELETE triggers.
                # Every unique identity matters, not only conventional id/uid.
                # A private custody row may be keyed by an episode or compound
                # receipt. SQL equality uses the column's declared collation.
                groups = []
                primary = [r[1] for r in conn.execute(f"PRAGMA table_info({quoted})") if r[5]]
                if primary:
                    groups.append([(col, None) for col in primary])
                for index in conn.execute(f"PRAGMA index_list({quoted})"):
                    if not index[2]:
                        continue
                    index_name = '"' + index[1].replace('"', '""') + '"'
                    cols = [(r[2], r[4]) for r in conn.execute(f"PRAGMA index_xinfo({index_name})") if r[5]]
                    if not cols or any(col is None for col, _ in cols):
                        raise ValueError("unsupported expression identity in home custody")
                    groups.append(cols)
                conflicts = []
                for cols in groups:
                    parts = []
                    for col, collation in cols:
                        name = '"' + col.replace('"', '""') + '"'
                        coll = '' if collation is None else ' COLLATE "' + collation.replace('"', '""') + '"'
                        parts.append(f'{name}{coll}=NEW.{name}{coll}')
                    conflicts.append('(' + ' AND '.join(parts) + ')')
                if conflicts:
                    old_ids = ("id",) if table == "tasks" else ("parent_id", "child_id") if table == "task_links" else ("task_id",)
                    for old_id in old_ids:
                        tests.append(f"task_id IN (SELECT {old_id} FROM {quoted} WHERE {' OR '.join(conflicts)})")
            cond = " OR ".join(tests)
            trigger = '"home_prepared1_' + table.replace('"', '""') + '_' + action.lower() + '"'
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {trigger}
                BEFORE {action} ON {quoted} WHEN EXISTS(SELECT 1 FROM {_TABLE} WHERE ({cond})
                    AND NOT EXISTS(SELECT 1 FROM kanban_home_finalized1 f
                                   WHERE f.task_id={_TABLE}.task_id AND f.nonce={_TABLE}.nonce)
                    AND NOT EXISTS(SELECT 1 FROM kanban_home_aborted1 a WHERE {abort.predicate()}))
                BEGIN SELECT RAISE(ABORT, 'home transfer prepared'); END""")
    for action in ("UPDATE", "DELETE"):
        condition = ("WHEN NOT EXISTS(SELECT 1 FROM kanban_home_aborted1 a WHERE a.task_id=OLD.task_id AND a.nonce=OLD.nonce)"
                     if action == "DELETE" else "")
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS home_prepared1_marker_{action.lower()}
            BEFORE {action} ON {_TABLE} {condition}
            BEGIN SELECT RAISE(ABORT, 'home transfer preparation immutable'); END""")
    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS home_prepared1_marker_insert
        BEFORE INSERT ON {_TABLE} WHEN EXISTS(SELECT 1 FROM {_TABLE} WHERE task_id=NEW.task_id)
        BEGIN SELECT RAISE(ABORT, 'home transfer preparation immutable'); END""")


def prepare(conn: sqlite3.Connection, task_id: str, *, source: str, destination: str,
            nonce: str, expected_version: int) -> dict:
    """Fence one idle local task inside the caller's write transaction.

    An exact retry returns the existing immutable tuple even after reopening.
    Refusals roll back this helper even when caught by a caller which commits.
    Outbox drainage is conservative across the entire board, including rejected
    custody and synthetic parents. This does not attest remote receipt/drainage.
    """
    if not conn.in_transaction:
        raise ValueError("home preparation requires a write transaction")
    if (not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", task_id)
            or not isinstance(source, str) or not _SOURCE.fullmatch(source)
            or destination != 'hub' or not isinstance(nonce, str) or not _NONCE.fullmatch(nonce)
            or type(expected_version) is not int or expected_version < 1):
        raise ValueError("invalid home preparation identity")
    record = dict(task_id=task_id, source=source, destination=destination,
                  nonce=nonce, expected_version=expected_version)
    conn.execute("SAVEPOINT home_prepare")
    try:
        _install(conn)
        old = conn.execute(f"SELECT task_id,source,destination,nonce,expected_version FROM {_TABLE} WHERE task_id=?",
                           (task_id,)).fetchone()
        if old and tuple(old) != tuple(record.values()) and abort.aborted(conn, task_id):
            conn.execute(f"DELETE FROM {_TABLE} WHERE task_id=?", (task_id,))
            old = None
        if old:
            if tuple(old) != tuple(record.values()):
                raise ValueError("home preparation identity changed")
        else:
            state = conn.execute("SELECT gateway_id FROM kanban_sync_state").fetchall()
            if len(state) != 1 or state[0][0] != source:
                raise ValueError("home preparation is not on the source gateway")
            row = conn.execute("SELECT created_on,sync_version,status,claim_lock,worker_pid,lease_holder,"
                               "workspace_custody FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row[0] != source or row[1] != expected_version:
                raise ValueError("home preparation version or source changed")
            if row[2] in ("running", "done", "archived") or any(row[i] is not None for i in (3, 4, 5, 6)):
                raise ValueError("home preparation requires an idle open task without custody")
            if conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL LIMIT 1", (task_id,)).fetchone():
                raise ValueError("home preparation has an unended run")
            if (conn.execute("SELECT 1 FROM kanban_sync_outbox LIMIT 1").fetchone()
                    or conn.execute("SELECT 1 FROM kanban_sync_parent_envelopes LIMIT 1").fetchone()
                    or conn.execute("SELECT 1 FROM kanban_sync_omissions LIMIT 1").fetchone()):
                raise ValueError("home preparation requires drained pending custody")
            from hermes_cli import kanban_home_memo as memo
            if memo.pending(conn, task_id):
                raise ValueError("home preparation requires drained administrative intent")
            if (conn.execute("SELECT 1 FROM sqlite_master WHERE name='containment_delivery'").fetchone()
                    and conn.execute("SELECT 1 FROM containment_delivery WHERE confirmed=0 AND "
                        "(json_extract(op_json,'$.task')=? OR json_extract(op_json,'$.actor_task')=?)", (task_id, task_id)).fetchone()):
                raise ValueError("home preparation requires drained containment intent")
            from hermes_cli.kanban_home_probe import require_source_probe
            require_source_probe(conn, task_id, source)
            conn.execute(f"INSERT INTO {_TABLE}(task_id,source,destination,nonce,expected_version) VALUES (?,?,?,?,?)",
                         tuple(record.values()))
    except BaseException:
        conn.execute("ROLLBACK TO home_prepare")
        conn.execute("RELEASE home_prepare")
        raise
    conn.execute("RELEASE home_prepare")
    return {"phase": "prepared_local", **record}


def frozen(conn: sqlite3.Connection, task_id: str) -> bool:
    """Read-only candidate exclusion; marker presence is not release authority.

    A malformed/orphan marker stays frozen. Only a scoped finalized receipt plus
    the installed local home/version/epoch makes the former source executable
    again under its ordinary future lease. SQL mutation fences remain separate.
    """
    import json
    from hermes_cli.kanban_home_generation import pending

    if pending(conn, task_id):
        return True
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)).fetchone():
        return False
    row = conn.execute(f"SELECT task_id,source,destination,nonce,expected_version FROM {_TABLE} WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        return False
    if abort.aborted(conn, task_id):
        return False
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_home_finalized1'").fetchone():
        return True
    final = conn.execute("SELECT nonce,receipt FROM kanban_home_finalized1 WHERE task_id=?", (task_id,)).fetchone()
    if not final:
        return True
    try:
        receipt = json.loads(final[1])
        keys = ("task", "source", "destination", "nonce", "expected_version")
        if (not isinstance(receipt, dict) or tuple(row) != tuple(receipt.get(k) for k in keys)
                or receipt.get("phase") != "materialized" or final[0] != row[3]
                or type(row[4]) is not int or row[4] < 1 or not _NONCE.fullmatch(row[3])
                or not isinstance(receipt.get("rows"), list) or not receipt["rows"]):
            return True
        current = conn.execute("SELECT created_on,sync_version FROM tasks WHERE id=?", (task_id,)).fetchone()
        epoch = conn.execute("SELECT nonce FROM kanban_home_generations1 WHERE task_id=?", (task_id,)).fetchone()
        return not (current and current[0] == row[2] and type(current[1]) is int
                    and current[1] >= row[4] + 1 and epoch and epoch[0] == row[3])
    except (ValueError, TypeError, sqlite3.Error):
        return True


def finalize_source(conn: sqlite3.Connection, record: dict) -> dict:
    """Stop local ownership after the authenticated destination materialization ACK."""
    from hermes_cli.kanban_home_source_stop import finalize_source as finish

    return finish(conn, record)


def abort_source(conn: sqlite3.Connection, record: dict) -> dict:
    """Record only an exact authenticated Board noncommit decision."""
    return abort.abort_source(conn, record)
