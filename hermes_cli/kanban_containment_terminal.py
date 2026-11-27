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
    from hermes_cli import kanban_containment_human as human
    data, frozen = _payload(event), _stop(stop)
    if not _uid(event.get("uid")) or event.get("task_id") != stop.get("task_id") or frozen is None or data is None:
        return False
    if data.get("reason") == human.CLOSURE:  # a human operator closed it; the authority is whatever it was
        return (human.valid_closure(data) and type(data["version"]) is int and data["version"] == 1
                and data["episode"] == stop["uid"] and data["containment_sha256"] == fingerprint(frozen)
                and data["authority"] == frozen["policy"]["authority"] and data["terminal_status"] == "archived")
    if data.keys() != {"version", "episode", "containment_sha256", "reason", "authority", "authority_status", "terminal_status"}:
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


def archive_local(conn, task, *, comment=None, precondition=None, via=None, event_extra=None, human=None):
    """``human`` (:class:`kanban_containment_human.HumanArchive`): a human operator closes every active episode
    whatever its authority's status, unless an authority has a live run or claim or a safety restriction holds it."""
    from hermes_cli import kanban_containment_human as operator
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
        if human is not None:
            if refusal := operator.safety_refusal(conn, task):
                raise life.ContainmentRefused(refusal)
            if row["containment_episode"] not in hold.episodes:
                raise life.ContainmentRefused("containment_episode_changed: the card's marker is not an active episode")
        closes = []
        for stop in (e for e in events if e["kind"] == "contained" and e["uid"] in hold.episodes):
            frozen = _stop(stop)
            attested = conn.execute("SELECT task_id,sha256 FROM containment_local_stops WHERE uid=?", (stop["uid"],)).fetchone()
            if frozen is None or attested is None or tuple(attested) != (task, fingerprint(frozen)):
                raise life.ContainmentRefused("containment stop is not exactly attested")
            authority = frozen["policy"]["authority"]
            if authority["board"] != frozen["target"]["board"] or authority["gateway"] != frozen["target"]["gateway"]:
                raise life.ContainmentRefused("local authority identity differs")
            at = conn.execute("SELECT * FROM tasks WHERE id=?", (authority["task_id"],)).fetchone()
            status = at["status"] if at else None
            base = {"version": 1, "episode": stop["uid"], "containment_sha256": fingerprint(frozen),
                    "authority": authority, "authority_status": status, "terminal_status": "archived"}
            if human is not None:
                if at is None:
                    raise life.ContainmentRefused("frozen authority is not a task on this board")
                if operator.authority_live(at):
                    raise life.ContainmentRefused(f"authority_live: authority {authority['task_id']} has a live run "
                                                  "or claim; let it release the card")
                closes.append((stop, operator.closure_payload(base, human, frozen["target"]["gateway"])))
                continue
            if status != "archived" and (status != "done" or "producer" in frozen["policy"]):
                raise life.ContainmentRefused("authority is neither archived nor done without producer; "
                                              + operator.hint(task))
            closes.append((stop, {**base, "reason": "authority_archived" if status == "archived"
                                  else "authority_done_without_producer"}))
        if not closes:
            raise life.ContainmentRefused("no active episode to close")
        with writer(conn, task), reason_comment(conn, task, comment) as noted:
            for stop, payload in closes:
                uid = life._append(conn, task, "containment_closed", payload)
                conn.execute("INSERT INTO containment_attestations VALUES (?,?,?,?,?)",
                             (task, uid, stop["uid"], fingerprint(payload), fingerprint(_stop(stop))))
            conn.execute("UPDATE tasks SET status='archived' WHERE id=? AND status='triage'", (task,))
            kb._append_event(conn, task, "archived", {"source_status": "triage", **({"via": via} if via else {}), **(event_extra or {}),
                                                      **(operator.audit(human, closes[0][1]["actor_gateway"]) if human else {})})
            noted.keep()
    kb.recompute_ready(conn)
    return True


def parent_status_sql(conn, alias="", *, table="tasks"):
    """Legacy schemas lack the marker; ordinary archived parents remain successful."""
    from hermes_cli.kanban_table_columns import table_columns  # one row, not one per column
    prefix = alias + "." if alias else ""
    if "containment_episode" not in table_columns(conn, table):
        return prefix + "status"
    return f"CASE WHEN {prefix}status='archived' AND {prefix}containment_episode IS NOT NULL THEN 'triage' ELSE {prefix}status END"
