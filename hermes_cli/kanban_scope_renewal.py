"""Authority-bound renewal of ONE scope-stopped episode. [daveey]

A worker's ``kanban_retain(reason='scope_stopped')`` restricts its card: no promotion, comment, timer or review
releases it (``kanban_spend_decision``). ``scope_renewed`` is the one release of that restriction, and only of it:

* it names one stop episode (the ``spend_budget_triaged`` uid) and one mandate comment, written after the stop by
  the same authority that renews. There are four: a human operator, the card's owning chat (an unreleased
  ``created`` chat link; ``shepherd``/``touch`` observers never qualify), the card's creator task, or a delegate (a
  chat session a human granted renewal rights, ``kanban_scope_delegate``). Runs of the stopped card never renew;
* it freezes the exact scope evidence it covers (``covered_restriction_uids``). Execution readers ignore covered
  evidence only; evidence added later, a new stop, and every safety restriction stay restrictive;
* it does not promote, claim or touch spend: the card stays in triage under its pending decision until a
  ``hermes kanban promote`` recorded AFTER the renewal, and ``kanban_spend_budget`` keeps the historical floor
  (``kanban_spend_decision.restricted_floor``), so a renew-then-promote cycle cannot open a fresh window.

A mandate edited after the renewal no longer matches its frozen digest: the renewal stops covering (restricted
again) and a fresh mandate plus a new renewal is the recovery.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

EVENT = "scope_renewed"
STOP, RESTRICT = "spend_budget_triaged", "decision_hold_restricted"
MAX_MANDATE = 8000  # the operator courier's comment bound: one limit on every path
MAX_REASON = 2000
ROUTE = ("record the renewed scope as a comment on the card, then "
         "`hermes kanban renew-scope {task} --episode <stop uid> --mandate-comment <comment uid or local id> --reason <text>` "
         "(a human operator, the card's owning chat, its creator task or a delegate chat), then a separate "
         "`hermes kanban promote {task} <what changed>`")


class RenewalRefused(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Actor:
    """Who renews, as the comment classifier sees them. ``authority``: human | chat | creator_task | delegate
    (``ref``: the grant uid)."""
    kind: str
    session: Optional[str]
    authority: str
    ref: Optional[str] = None
    gateway: str = ""
    profile: str = "default"


def key(event: dict) -> str:
    return event.get("uid") or str(event["id"])


def _payload(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def comment_ref(row: dict) -> str:
    """Portable identity of a comment: its sync uid, else the local id on a board that never synced."""
    return row.get("uid") or str(row["id"])


def _comment(conn: sqlite3.Connection, task_id: str, ref: str) -> Optional[dict]:
    if not ref:
        return None
    if "uid" in _columns(conn, "task_comments"):
        row = conn.execute("SELECT * FROM task_comments WHERE task_id=? AND uid=?", (task_id, ref)).fetchone()
        if row is not None:
            return dict(row)
    if ref.isascii() and ref.isdecimal():
        row = conn.execute("SELECT * FROM task_comments WHERE task_id=? AND id=?", (task_id, int(ref))).fetchone()
        if row is not None and comment_ref(dict(row)) == ref:
            return dict(row)
    return None


def renewals(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """Renewals whose frozen mandate is still the comment on the card; each with a parsed ``payload``."""
    out = []
    for row in conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind=?", (task_id, EVENT)):
        event = dict(row)
        p = _payload(event["payload"])
        uids = p.get("covered_restriction_uids")
        if p.get("version") != 1 or not p.get("episode") or not isinstance(uids, list):
            continue
        mandate = _comment(conn, task_id, str(p.get("mandate_comment_uid") or ""))
        if mandate is None or digest(mandate["body"]) != p.get("mandate_body_sha256"):
            continue
        out.append({**event, "payload": p})
    return out


def covered(conn: sqlite3.Connection, task_id: str) -> frozenset:
    """Keys of scope evidence a valid renewal froze. Callers never apply it to safety evidence."""
    return frozenset(str(uid) for r in renewals(conn, task_id) for uid in r["payload"]["covered_restriction_uids"])


def reopened(conn: sqlite3.Connection, task_id: str, ordinary: list[dict]) -> Optional[dict]:
    """The renewed stop that still waits for its own decision.

    A renewal re-opens its stop at the renewal's position: only a promotion or archive recorded after it is that
    decision. Without this, a promotion older than the scope annotation would release the card at renewal.
    "After" is arrival order (the row id), as on the Board (its ``ord``): an event's ``created_at`` and uid are
    its writer's clock, and a promotion recorded before the renewal must not decide it whatever time it states.
    On a synced board a replica's row id is its own arrival order, not the Board's: a promotion another gateway
    recorded before the renewal can be pulled after it. This reader may then show the stop decided; execution
    still needs the Board's lease, and the Board decides by its own ``ord`` (``spend_decision_pending``).
    ``ordinary``: stop/promotion/archive events.
    """
    renewed = sorted(renewals(conn, task_id), key=lambda e: int(e["id"]), reverse=True)
    if not renewed:
        return None
    decided = max((int(e["id"]) for e in ordinary if e["kind"] != STOP), default=-1)
    for renewal in renewed:
        if decided > int(renewal["id"]):
            return None
        stop = next((e for e in ordinary if e["kind"] == STOP and key(e) == renewal["payload"]["episode"]), None)
        if stop is not None:
            return stop
    return None


def scope_evidence(conn: sqlite3.Connection, task_id: str, episode: str) -> list[dict]:
    """Scope-cause stop/annotation rows and confirmed scope receipts bound to ``episode``. Never safety."""
    from hermes_cli.kanban_spend_decision import receipt_restrictions
    out = []
    for row in conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind IN (?,?)", (task_id, STOP, RESTRICT)):
        event = dict(row)
        p = _payload(event["payload"])
        if p.get("cause") == "scope_stopped" and (key(event) if event["kind"] == STOP else p.get("episode")) == episode:
            out.append(event)
    for event in receipt_restrictions(conn, task_id):
        p = _payload(event["payload"])
        if p.get("superseded_by_safety_stop") is not True and p.get("episode_uid") == episode:
            out.append(event)
    return out


def _mandate_event(conn: sqlite3.Connection, task_id: str, comment: dict) -> Optional[dict]:
    """The one ``commented`` event bound to this comment (uid on synced boards, local id otherwise)."""
    found = []
    for row in conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='commented'", (task_id,)):
        event = dict(row)
        p = _payload(event["payload"])
        bound = (p.get("comment_uid") == comment["uid"] if comment.get("uid")
                 else p.get("comment_uid") is None and p.get("comment_id") == comment["id"])
        if bound:
            found.append({**event, "payload": p})
    return found[0] if len(found) == 1 else None


def _creator_sessions(conn: sqlite3.Connection, creator: str) -> set:
    return {str(s) for (raw,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='owner_admitted'", (creator,))
        if (s := _payload(raw).get("session_id"))}


def _writer_refusal(conn: sqlite3.Connection, actor: Actor, event: dict) -> Optional[str]:
    """None when the mandate was written by the renewing authority, on what the ``commented`` event records."""
    p = event["payload"]
    if actor.authority == "human":
        if p.get("writer_kind") != "human":
            return "a human operator renews on a mandate comment a human wrote"
        if actor.gateway and event.get("origin_gateway") and event["origin_gateway"] != actor.gateway:
            return f"the mandate comment was written on {event['origin_gateway']}, not this gateway"
        return None
    if p.get("writer_kind") != "agent":
        return "the mandate comment was not written by the renewing agent"
    if actor.authority == "creator_task":
        if p.get("writer_task") == actor.ref or p.get("writer_session") in _creator_sessions(conn, str(actor.ref)):
            return None
        return f"the mandate comment was not written by creator task {actor.ref}"
    if p.get("writer_task") or not actor.session or p.get("writer_session") != actor.session:
        return ("the mandate comment was not written by this delegate session" if actor.authority == "delegate"
                else "the mandate comment was not written by this owning chat")
    return None


def _mandate_comment(conn: sqlite3.Connection, task_id: str, ref: str | int) -> Optional[dict]:
    """Resolve caller input, allowing local IDs without relaxing frozen renewal UID readers."""
    ref = str(ref)
    comment = _comment(conn, task_id, ref)
    if comment is None and ref.isascii() and ref.isdecimal():
        row = conn.execute("SELECT * FROM task_comments WHERE task_id=? AND id=?", (task_id, ref)).fetchone()
        comment = dict(row) if row is not None else None
    return comment


def prepare(conn: sqlite3.Connection, task_id: str, *, episode: str, comment_id: str | int, reason: str,
            actor: Actor) -> dict:
    """The ``scope_renewed`` payload, or :class:`RenewalRefused`. Call inside the write transaction that appends it."""
    from hermes_cli import kanban_spend_decision as ksd
    from hermes_cli.kanban_refusal_receipt import restricted as safety_restricted
    from hermes_cli.kanban_sync_divergence import _event_order
    reason = (reason or "").strip()
    if not reason or len(reason) > MAX_REASON:
        raise RenewalRefused("invalid_reason", f"--reason is required (at most {MAX_REASON} characters)")
    task = conn.execute("SELECT status, current_run_id, claim_lock FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        raise RenewalRefused("no_such_task", f"no such task {task_id}")
    if safety_restricted(conn, task_id):
        raise RenewalRefused("safety_restricted", ksd.execution_refusal(conn, task_id) or "safety restriction retained")
    if task[0] != "triage" or task[2]:
        raise RenewalRefused("not_stopped", f"{task_id} is {task[0]}, not held in triage by a scope stop")
    stop = ksd.pending_episode(conn, task_id)
    current = key(stop) if stop else None
    if stop is None or stop["kind"] != STOP or current != episode:
        raise RenewalRefused("episode_mismatch", f"episode {episode} is not {task_id}'s current stop ({current}); "
                             "reread the card")
    done = covered(conn, task_id)
    evidence = [e for e in scope_evidence(conn, task_id, episode) if key(e) not in done]
    if not evidence:
        raise RenewalRefused("not_scope_stopped", f"stop {episode} on {task_id} carries no unrenewed scope restriction")
    comment = _mandate_comment(conn, task_id, comment_id)
    body = (comment["body"] or "").strip() if comment is not None else ""
    if not body:
        raise RenewalRefused("mandate_missing", f"comment {comment_id} is not a non-empty comment on {task_id}")
    if len(body) > MAX_MANDATE:
        raise RenewalRefused("mandate_too_long", f"the mandate comment exceeds {MAX_MANDATE} characters; "
                             "write a shorter one")
    comment = dict(comment)
    written = _mandate_event(conn, task_id, comment)
    if written is None:
        raise RenewalRefused("mandate_unattributable", f"comment {comment_id} has no single writer record; "
                             "write a fresh mandate comment")
    if why := _writer_refusal(conn, actor, written):
        raise RenewalRefused("mandate_authority", why)
    if any(_event_order(e) >= _event_order(written) for e in evidence):
        raise RenewalRefused("mandate_stale", f"comment {comment_id} predates the scope stop it would renew; "
                             "write the mandate after the stop")
    return {"version": 1, "episode": episode, "mandate_comment_uid": comment_ref(comment),
            "mandate_comment_id": comment["id"], "mandate_event_uid": key(written),
            "mandate_body_sha256": digest(comment["body"]),
            "covered_restriction_uids": sorted(key(e) for e in evidence),
            "actor_kind": actor.kind, "actor_session": actor.session, "actor_gateway": actor.gateway,
            "actor_profile": actor.profile, "authority_kind": actor.authority, "authority_ref": actor.ref,
            "reason": reason}


def renew_local(conn: sqlite3.Connection, task_id: str, *, episode: str, comment_id: str | int, reason: str,
                actor: Actor) -> dict:
    """Validate and append in one write transaction on a board with no canonical Board. Status is untouched."""
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        payload = prepare(conn, task_id, episode=episode, comment_id=comment_id, reason=reason, actor=actor)
        kb._append_event(conn, task_id, EVENT, payload)
    return payload


def mandate(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """The newest effective renewal's mandate for the next run's context, or None while restricted."""
    from hermes_cli import kanban_spend_decision as ksd
    from hermes_cli.kanban_sync_divergence import _event_order
    valid = renewals(conn, task_id)
    if not valid or ksd.execution_restricted(conn, task_id):
        return None
    renewal = max(valid, key=_event_order)
    comment = _comment(conn, task_id, str(renewal["payload"]["mandate_comment_uid"]))
    return {**renewal["payload"], "body": comment["body"], "mandate_comment_id": comment["id"]} if comment else None
