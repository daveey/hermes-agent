"""Transaction owner for local non-human containment.

Shared by CLI/tools through the admitted-agent service. Synced boards deliberately
refuse this local-only owner: local attestations cannot replace Board custody.
"""
from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping
from typing import Any

from hermes_cli import kanban_containment_guard as guard
from hermes_cli.kanban_containment_policy import identity, policy
from hermes_cli.kanban_containment_state import _payload, _stop, _uid, fingerprint, reduce_events


class ContainmentRefused(ValueError):
    """No authority, stale evidence, or unsupported synced capability."""

    code = "containment_refused"


class ContainmentConflict(ContainmentRefused):
    """A deterministic evidence conflict, never a transient one: the Board's authenticated evidence for a receipt
    no longer equals this host's first certified observation (``identity_changed``), or a feed event differs from
    its canonical receipt (``event_differs``). Only these quarantine one task (``kanban_sync_quarantine``); every
    other refusal (unproven or non-live evidence, unavailable proof, an invalid page) refuses the whole page.
    ``uid`` is the receipt whose evidence conflicted (a release's stop receipt can be the one). [daveey]"""

    def __init__(self, message: str, *, kind: str, uid: str | None = None):
        super().__init__(message)
        self.kind, self.uid = kind, uid


def _local(conn: sqlite3.Connection) -> None:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_sync_state'").fetchone()
    if exists and conn.execute("SELECT 1 FROM kanban_sync_state LIMIT 1").fetchone():
        raise ContainmentRefused("synced containment capability is not installed")


def install_local(conn: sqlite3.Connection) -> None:
    """Install a local-only immutable verification ledger and storage backstop.

    No network calls are made from SQLite UDFs. Successful local authorization is
    committed with its release, then read by exact event/predicate fingerprints.
    This ledger MUST NOT be imported as an authenticated remote proof.
    """
    _local(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS containment_attestations ("
                 "task_id TEXT NOT NULL, receipt_uid TEXT PRIMARY KEY, episode_uid TEXT NOT NULL, "
                 "receipt_sha256 TEXT NOT NULL, episode_sha256 TEXT NOT NULL)")
    guard.install(conn, verify_release=lambda receipt, stop: verified_release(conn, receipt, stop))
    for op in ("UPDATE", "DELETE"):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS containment_attestation_{op.lower()} "
                     f"BEFORE {op} ON containment_attestations BEGIN SELECT RAISE(ABORT,'containment_pending'); END")
    conn.execute("CREATE TRIGGER IF NOT EXISTS containment_attestation_insert "
                 "BEFORE INSERT ON containment_attestations WHEN NOT kanban_containment_write(NEW.task_id) "
                 "BEGIN SELECT RAISE(ABORT,'containment_pending'); END")


def verified_release(conn: sqlite3.Connection, receipt: Mapping[str, Any], stop: Mapping[str, Any]) -> bool:
    """Local durable attestation; altered payloads and remapped IDs cannot match."""
    data, frozen = _payload(receipt), _stop(stop)
    if data is None or frozen is None:
        return False
    row = conn.execute("SELECT task_id,episode_uid,receipt_sha256,episode_sha256 "
                       "FROM containment_attestations WHERE receipt_uid=?", (receipt.get("uid"),)).fetchone()
    return bool(row and tuple(row) == (receipt.get("task_id"), stop.get("uid"), fingerprint(data), fingerprint(frozen)))


def _events(conn: sqlite3.Connection, task: str) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM task_events WHERE task_id=? "
                                             "AND kind IN ('contained','containment_released')", (task,))]


def _actor(conn: sqlite3.Connection, task: str, run_uid: str) -> None:
    if not _uid(run_uid):
        raise ContainmentRefused("actor requires a portable current run identity")
    row = conn.execute("SELECT t.status,t.current_run_id,r.id,r.ended_at,r.claim_expires,"
                       "t.claim_expires,t.claim_lock,r.claim_lock "
                       "FROM tasks t JOIN task_runs r ON r.task_id=t.id WHERE t.id=? AND r.uid=?",
                       (task, run_uid)).fetchone()
    now = time.time()
    if (row is None or row[0] != "running" or row[1] != row[2] or row[3] is not None
            or row[4] is None or row[4] <= now or row[5] is None or row[5] <= now
            or not row[6] or row[6] != row[7]):
        raise ContainmentRefused("actor is not the current live claimed run")


def _reachable(conn: sqlite3.Connection, parent: str, child: str) -> bool:
    return conn.execute("WITH RECURSIVE descendants(id) AS (SELECT child_id FROM task_links WHERE parent_id=? "
                        "UNION SELECT l.child_id FROM task_links l JOIN descendants d ON l.parent_id=d.id) "
                        "SELECT 1 FROM descendants WHERE id=? LIMIT 1", (parent, child)).fetchone() is not None


def _nominees(conn: sqlite3.Connection, target: dict, spec: dict) -> None:
    for key in ("authority", "producer"):
        nominee = spec.get(key)
        if nominee is None:
            continue
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (nominee["task_id"],)).fetchone()
        if row is None or row[0] == "archived" or _reachable(conn, target["task_id"], nominee["task_id"]):
            raise ContainmentRefused(f"{key} must exist independently of the contained task")


def _baseline(conn: sqlite3.Connection, spec: dict) -> dict | None:
    """Freeze the newest run even when it has not completed at containment."""
    producer = spec.get("producer")
    if producer is None:
        return None
    row = conn.execute("SELECT id,uid FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (producer["task_id"],)).fetchone()
    if row is None:
        return None
    if not _uid(row["uid"]):
        raise ContainmentRefused("producer baseline lacks a portable run identity")
    event = conn.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='completed' AND run_id=? "
                         "ORDER BY id DESC LIMIT 1", (producer["task_id"], row["id"])).fetchone()
    if event is not None and not _uid(event[0]):
        raise ContainmentRefused("producer baseline lacks a portable event identity")
    return {"run_uid": row["uid"], "event_uid": event[0] if event else None}


def _producer(conn: sqlite3.Connection, spec: dict) -> dict | None:
    producer = spec.get("producer")
    if producer is None:
        return None
    row = conn.execute("SELECT e.id,e.uid,r.uid AS run_uid,t.status,r.outcome,r.ended_at "
                       "FROM tasks t JOIN task_events e ON e.task_id=t.id "
                       "JOIN task_runs r ON r.id=e.run_id AND r.task_id=t.id "
                       "WHERE t.id=? AND e.kind='completed' ORDER BY e.id DESC LIMIT 1",
                       (producer["task_id"],)).fetchone()
    if row is None:
        return None
    if row["status"] != "done" or row["outcome"] != "completed" or row["ended_at"] is None:
        return None
    if not _uid(row["uid"]) or not _uid(row["run_uid"]):
        raise ContainmentRefused("producer terminal evidence lacks portable identities")
    # A newer run (even failed) makes the old completion stale; don't bless an old
    # completed event solely because somebody reset the task row to done.
    latest = conn.execute("SELECT id FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                          (producer["task_id"],)).fetchone()
    event_run = conn.execute("SELECT run_id FROM task_events WHERE uid=?", (row["uid"],)).fetchone()
    if latest is None or latest[0] != event_run[0]:
        return None
    return {"event_uid": row["uid"], "run_uid": row["run_uid"], "local_event_id": row["id"]}


def _append(conn: sqlite3.Connection, task_id: str, kind: str, payload: dict) -> str:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_sync import uuid7
    kb._append_event(conn, task_id, kind, payload)
    row = conn.execute("SELECT id,uid,payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (task_id,)).fetchone()
    if _payload(dict(row)) != payload:
        raise ContainmentRefused("unsupported containment event envelope")
    uid = row["uid"] or uuid7()
    if row["uid"] is None:
        conn.execute("UPDATE task_events SET uid=? WHERE id=?", (uid, row["id"]))
    return uid


def contain_local(conn: sqlite3.Connection, *, target: dict, resume_policy: dict,
                  actor_task: str, actor_run_uid: str, summary: str) -> dict:
    """End an own run or contain an unclaimed descendant; no dependency release.

    Only a live ancestor may contain a never-dispatched relay. The frozen resume
    authority must not depend (even transitively) on that relay.
    """
    from hermes_cli import kanban_db as kb
    target = identity(target)
    spec = policy(resume_policy, target=target)
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
        raise ContainmentRefused("containment requires a bounded handoff summary")
    install_local(conn)
    tid = target["task_id"]
    with kb.write_txn(conn):
        _local(conn)
        _actor(conn, actor_task, actor_run_uid)
        row = conn.execute("SELECT status,current_run_id,human_request FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None or row[2] is not None:
            raise ContainmentRefused("containment cannot replace an unknown task or human hold")
        if actor_task == tid:
            if row[0] != "running":
                raise ContainmentRefused("own containment requires the current running task")
        elif row[0] not in ("todo", "ready", "triage") or row[1] is not None or not _reachable(conn, actor_task, tid):
            raise ContainmentRefused("foreign containment requires an unclaimed descendant")
        if guard.pending(conn, tid, verify_release=lambda r, s: verified_release(conn, r, s)):
            raise ContainmentRefused("task is already contained; resume its exact episode")
        from hermes_cli.kanban_containment_authority import require_open
        authority = conn.execute("SELECT status FROM tasks WHERE id=?", (spec["authority"]["task_id"],)).fetchone()
        require_open(authority[0] if authority else None)
        _nominees(conn, target, spec)
        baseline = _baseline(conn, spec)
        payload = {"version": 1, "target": target, "policy": spec, "producer_baseline": baseline}
        with guard.authorized_write(conn, tid):
            kb._end_or_synthesize_run(conn, tid, outcome="contained", status="contained", summary=summary,
                                      metadata={"containment": payload}, synthesize=False)
            conn.execute("UPDATE tasks SET status='triage',current_run_id=NULL,completed_at=NULL,"
                         "claim_lock=NULL,claim_expires=NULL,worker_pid=NULL,worker_started_at=NULL WHERE id=?", (tid,))
            episode = _append(conn, tid, "contained", payload)
            conn.execute("UPDATE tasks SET containment_episode=? WHERE id=?", (episode, tid))
    return {"task_id": tid, "status": "triage", "episode": episode, "resume_policy": spec}


def resume_local(conn: sqlite3.Connection, *, target: dict, episode: str, actor_task: str,
                 actor_run_uid: str, kind: str) -> dict:
    """Resume an exact episode on scoped authority OR a new completed producer run.

    An explicit call is the event producer: no periodic polling or timer wake.
    Unknown, stale and offline/synced evidence refuses without recording release.
    """
    from hermes_cli import kanban_db as kb
    target = identity(target)
    install_local(conn)
    tid = target["task_id"]
    with kb.write_txn(conn):
        _local(conn)
        _actor(conn, actor_task, actor_run_uid)
        events = _events(conn, tid)
        hold = reduce_events(events, verify_release=lambda r, s: verified_release(conn, r, s))
        stop = next((e for e in events if e["kind"] == "contained" and e["uid"] == episode), None)
        if hold is None or hold.unresolved or episode not in hold.episodes or stop is None:
            raise ContainmentRefused("unknown, stale or unresolved containment episode")
        frozen = _stop(stop)
        if frozen is None or frozen["target"] != target:
            raise ContainmentRefused("containment target or frozen predicate changed")
        _nominees(conn, target, frozen["policy"])
        row = conn.execute("SELECT status,current_run_id,human_request FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None or row[0] != "triage" or row[1] is not None or row[2] is not None:
            raise ContainmentRefused("contained task changed custody")
        if kind == "authority":
            actor = frozen["policy"]["authority"]
            if actor["task_id"] != actor_task:
                raise ContainmentRefused("caller is not the episode's scoped authority")
            evidence = {"kind": kind, kind: actor, "run_uid": actor_run_uid}
        elif kind == "producer":
            producer = _producer(conn, frozen["policy"])
            baseline = frozen["producer_baseline"]
            if producer is None or producer["local_event_id"] <= stop["id"] or (baseline is not None and any(
                    producer[k] == baseline[k] for k in ("event_uid", "run_uid"))):
                raise ContainmentRefused("producer requires genuinely new completed-run evidence")
            evidence = {"kind": kind, kind: frozen["policy"][kind],
                        **{k: producer[k] for k in ("event_uid", "run_uid")}}
        else:
            raise ContainmentRefused("unsupported resume predicate")
        payload = {"version": 1, "episode": episode, "containment_sha256": fingerprint(frozen),
                   "resume_status": "ready", "evidence": evidence}
        with guard.authorized_write(conn, tid):
            # Receipt and attestation share one transaction. An append, ledger
            # or landing error rolls both back; no permission escapes a failure.
            receipt_uid = _append(conn, tid, "containment_released", payload)
            conn.execute("INSERT INTO containment_attestations VALUES (?,?,?,?,?)",
                         (tid, receipt_uid, episode, fingerprint(payload), fingerprint(frozen)))
            remaining = reduce_events(_events(conn, tid), verify_release=lambda r, s: verified_release(conn, r, s))
            from hermes_cli.kanban_spend_decision import pending as spend_pending
            landing = "triage" if remaining is not None or spend_pending(conn, tid) else kb._landing_status_after_parents(conn, tid)
            conn.execute("UPDATE tasks SET status=?,containment_episode=CASE WHEN ? THEN containment_episode ELSE NULL END "
                         "WHERE id=?", (landing, remaining is not None, tid))
    return {"task_id": tid, "status": landing, "episode": episode, "release": receipt_uid}
