"""Terminal non-success containment closure, deliberately separate from release."""
from __future__ import annotations

import contextlib
import contextvars
import sqlite3

from hermes_cli.kanban_containment_state import _payload, _stop, _uid, fingerprint, reduce_events

_SCOPE = contextvars.ContextVar("containment_terminal_write", default=None)


@contextlib.contextmanager
def writer(conn, task):
    """A connection/task/transaction scoped archived-only writer, never readiness."""
    if not conn.in_transaction:
        raise ValueError("terminal writer requires a transaction")
    token = _SCOPE.set((id(conn), task))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def permitted(conn, task):
    return int(conn.in_transaction and _SCOPE.get() == (id(conn), task))


def closure(event, stop):
    data, frozen = _payload(event), _stop(stop)
    if (not _uid(event.get("uid")) or event.get("task_id") != stop.get("task_id")
            or frozen is None or data is None or data.keys() != {
                "version", "episode", "containment_sha256", "reason", "authority", "authority_status", "terminal_status"}):
        return False
    status = data["authority_status"]
    reason = "authority_archived" if status == "archived" else "authority_done_without_producer"
    return (type(data["version"]) is int and data["version"] == 1 and data["episode"] == stop["uid"]
            and data["containment_sha256"] == fingerprint(frozen)
            and data["authority"] == frozen["policy"]["authority"] and data["terminal_status"] == "archived"
            and (status == "archived" or (status == "done" and "producer" not in frozen["policy"]))
            and data["reason"] == reason)


def terminal_closed(events, *, verify):
    """All still-held stops must have exact authenticated closures; hold remains."""
    events = list(events)
    hold = reduce_events(events, verify_release=verify)
    if hold is None or hold.unresolved or not hold.episodes:
        return False
    stops = {e.get("uid"): e for e in events if e.get("kind") == "contained"}
    return all(any(e.get("kind") == "containment_closed" and closure(e, stops[uid])
                   and verify(e, stops[uid]) is True for e in events) for uid in hold.episodes)


def closed(conn, task):
    from hermes_cli.kanban_containment_connections import verifier
    from hermes_cli.kanban_containment_lifecycle import _events
    from hermes_cli.kanban_containment_guard import pending
    events = _events(conn, task)
    # Missing marker/request observations cannot be overridden by a valid older closure.
    known = {e["uid"] for e in events if e["kind"] == "contained"}
    observed = {r[0] for r in conn.execute("SELECT episode FROM containment_observations WHERE task_id=?", (task,))}
    from hermes_cli.kanban_containment_requests import pending as request_pending
    return (not (observed - known) and not request_pending(conn, task, known) and pending(conn, task)
            and terminal_closed(events, verify=lambda r, s: verifier(conn, r, s)))


def archive_local(conn, task, *, comment=None, precondition=None, via=None, event_extra=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_containment_lifecycle as life
    from hermes_cli.kanban_reason_comment import reason_comment
    life.install_local(conn)
    with kb.write_txn(conn):
        if precondition is not None and precondition(conn):
            return False
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
        events = life._events(conn, task)
        hold = reduce_events(events, verify_release=lambda r, s: life.verified_release(conn, r, s))
        if (row is None or row["status"] != "triage" or hold is None or hold.unresolved
                or row["containment_episode"] is None
                or any(row[k] is not None for k in ("current_run_id", "claim_lock", "claim_expires", "human_request"))
                or ("delete_pending" in row.keys() and row["delete_pending"])):
            raise life.ContainmentRefused("containment target is not an unclaimed triage task")
        known = {e["uid"] for e in events if e["kind"] == "contained"}
        observed = {r[0] for r in conn.execute("SELECT episode FROM containment_observations WHERE task_id=?", (task,))}
        if observed - known:
            raise life.ContainmentRefused("unknown containment episode")
        from hermes_cli.kanban_containment_requests import pending as request_pending
        if request_pending(conn, task, known):
            raise life.ContainmentRefused("unknown containment request")
        closes = []
        for stop in (e for e in events if e["kind"] == "contained" and e["uid"] in hold.episodes):
            frozen = _stop(stop)
            attested = conn.execute("SELECT task_id,sha256 FROM containment_local_stops WHERE uid=?", (stop["uid"],)).fetchone()
            if frozen is None or attested is None or tuple(attested) != (task, fingerprint(frozen)):
                raise life.ContainmentRefused("containment stop is not exactly attested")
            authority = frozen["policy"]["authority"]
            if authority["board"] != frozen["target"]["board"] or authority["gateway"] != frozen["target"]["gateway"]:
                raise life.ContainmentRefused("local authority identity differs")
            at = conn.execute("SELECT status FROM tasks WHERE id=?", (authority["task_id"],)).fetchone()
            status = at[0] if at else None
            if status != "archived" and (status != "done" or "producer" in frozen["policy"]):
                raise life.ContainmentRefused("authority is neither archived nor done without producer")
            closes.append((stop, {"version": 1, "episode": stop["uid"], "containment_sha256": fingerprint(frozen),
                                  "authority": authority, "authority_status": status, "terminal_status": "archived",
                                  "reason": "authority_archived" if status == "archived" else "authority_done_without_producer"}))
        if not closes:
            raise life.ContainmentRefused("no active episode to close")
        with writer(conn, task), reason_comment(conn, task, comment) as noted:
            for stop, payload in closes:
                uid = life._append(conn, task, "containment_closed", payload)
                conn.execute("INSERT INTO containment_attestations VALUES (?,?,?,?,?)",
                             (task, uid, stop["uid"], fingerprint(payload), fingerprint(_stop(stop))))
            conn.execute("UPDATE tasks SET status='archived' WHERE id=? AND status='triage'", (task,))
            kb._append_event(conn, task, "archived", {"source_status": "triage", **({"via": via} if via else {}), **(event_extra or {})})
            noted.keep()
    kb.recompute_ready(conn)
    return True


_COVERED = "hermes_scope_renewal_covers"


def _register_covered(conn) -> bool:
    """Expose ``kanban_scope_renewal.covered`` to SQL on ``conn``; any read failure is uncovered (fail closed)."""
    from hermes_cli import kanban_scope_renewal as renewal

    def covers(task_id, key):
        # A reader schema without comments/renewal columns (minimal mirrors) cannot prove coverage: restrictive.
        try:
            return 1 if str(key) in renewal.covered(conn, str(task_id)) else 0
        except (sqlite3.Error, KeyError, IndexError, TypeError, ValueError):
            return 0
    try:
        sqlite3.Connection.create_function(conn, _COVERED, 2, covers)
    except (sqlite3.Error, TypeError):
        return False
    return True


def parent_status_sql(conn, alias="", *, table="tasks"):
    """Effective dependency state, never terminal success for an execution-restricted scope.

    Both full and incremental foreign mirrors use this expression. Legacy minimal
    schemas may lack history/marker columns; do not reference nonexistent columns.
    Ordinary archives remain successful; administrative archive does not clear a stop.
    """
    prefix = (alias or table) + "."
    clauses = []
    if "containment_episode" in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
        clauses.append(f"({prefix}status='archived' AND {prefix}containment_episode IS NOT NULL)")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(task_events)")}
    if {"task_id", "kind", "payload"} <= columns:
        live = " AND de.deleted_at IS NULL" if "deleted_at" in columns else ""
        cause = "CASE WHEN json_valid(de.payload) THEN json_extract(de.payload,'$.cause') END"
        # A scope stop a valid renewal froze (kanban_scope_renewal.covered: exact uid, unedited mandate) no longer
        # restricts, exactly as execution readers treat it; safety evidence is never renewable.
        renewed = ""
        if "id" in columns and _register_covered(conn):
            key = "COALESCE(de.uid, CAST(de.id AS TEXT))" if "uid" in columns else "CAST(de.id AS TEXT)"
            renewed = f" AND NOT ({cause}='scope_stopped' AND {_COVERED}(de.task_id, {key}))"
        clauses.append(f"EXISTS (SELECT 1 FROM task_events de WHERE de.task_id={prefix}id "
                       "AND de.kind IN ('spend_budget_triaged','decision_hold_restricted') "
                       f"AND {cause} IN ('safety_refusal','scope_stopped'){live}{renewed})")
    if not clauses:
        return prefix + "status"
    return (f"CASE WHEN {prefix}status IN ('done','archived') AND ({' OR '.join(clauses)}) "
            f"THEN 'triage' ELSE {prefix}status END")
