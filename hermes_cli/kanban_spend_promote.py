"""Spend-stop promotion over the authenticated operator courier. [daveey]

A card the cumulative spend budget stopped (``kanban_spend_budget``) waits in triage for a recorded decision
(``kanban_spend_decision``). When no chat owns that decision, the stop's producer receipt names one supervisor
card. That card's worker cannot run ``hermes kanban spend-budget`` / ``promote`` (worker shells are fenced from
board writes), and an operator ``unblock`` is not a decision. This is the one supported release:

    {"op": "operator", "action": "spend_promote", "task_id": <stopped card>, "runs": 1..MAX_RUNS,
     "episode": <uid of the current spend_budget_triaged event>,
     "decision_comment_id": <the operator comment that recorded the decision on that card>,
     "reason": <what changed>}

Scope, checked on the replica at request time and again on the home's live file inside the write transaction:
the target is in triage under a pending spend hold whose CURRENT stop is ``episode``; that stop's
``spend_budget_decision_ready`` producer is a supervisor and it is the calling card; the decision comment is the
caller's own operator comment on the target, written after the stop (event order, not just the second), and no
human has commented on the card since that decision comment; no human hold and no open parent. The
operator courier proves the caller (live run, Board lease holder, Board evidence) before the home applies it.

The home then, in ONE transaction: moves the card triage -> ready, records a ``spend_budget_set`` override of
exactly ``runs`` runs (the card's own cache-read-token override, if any, is kept), and a ``promoted_manual`` event
carrying ``spend_budget`` = {runs, episode, decision_comment_id}. That promotion starts the card's new spend
window (``kanban_spend_budget._window_start``), so the card gets exactly ``runs`` more runs before the budget
stops it again. Finite only: 0 (exempt) is refused. A replayed request is ``already_applied``.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

ACTION = "spend_promote"
FIELDS = frozenset({"runs", "episode", "decision_comment_id", "reason"})
MAX_RUNS = 20
WINDOW_KEY = "spend_budget"  # promoted_manual payload key that opens a new spend window


def _err(code: str, message: str):
    from hermes_cli.kanban_supervisor_requests import SupervisorRequestError
    return SupervisorRequestError(code, message)


def _pos_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdecimal():
        return int(value.strip())
    return None


def normalize(i: int, op: dict) -> dict:
    """The validated operator op, or ``SupervisorRequestError``."""
    from hermes_cli.kanban_operator_call import MAX_REASON, OP
    task_id = str(op.get("task_id") or "").strip()
    if not task_id:
        raise _err("invalid_ops", f"op {i}: task_id (the one spend-stopped card) is required")
    extra = set(op) - {"op", "action", "task_id"} - FIELDS
    if extra:
        raise _err("invalid_ops", f"op {i}: {ACTION} takes only task_id and {', '.join(sorted(FIELDS))}; "
                   f"unexpected {', '.join(sorted(extra))}")
    runs = _pos_int(op.get("runs"))
    if runs is None or not 1 <= runs <= MAX_RUNS:
        raise _err("invalid_ops", f"op {i}: runs must be a whole number from 1 to {MAX_RUNS} (a finite new window; "
                   f"no exemption), not {op.get('runs')!r}")
    episode = str(op.get("episode") or "").strip()
    if not episode:
        raise _err("invalid_ops", f"op {i}: episode (the uid of the card's current spend_budget_triaged event) "
                   "is required")
    comment = _pos_int(op.get("decision_comment_id"))
    if comment is None or comment < 1:
        raise _err("invalid_ops", f"op {i}: decision_comment_id (your operator comment on the card recording the "
                   "decision) is required")
    reason = str(op.get("reason") or "").strip()
    if not reason:
        raise _err("invalid_ops", f"op {i}: reason is required for {ACTION}")
    return {"op": OP, "action": ACTION, "task_id": task_id, "runs": runs, "episode": episode[:128],
            "decision_comment_id": comment, "reason": reason[:MAX_REASON]}


def _payload(raw: Any) -> dict:
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _current_stop(conn: sqlite3.Connection, target: str) -> Optional[dict]:
    from hermes_cli.kanban_spend_decision import EVENT
    from hermes_cli.kanban_sync_divergence import _event_order
    rows = [dict(r) for r in conn.execute("SELECT * FROM task_events WHERE task_id = ? AND kind = ?",
                                          (target, EVENT))]
    return max(rows, key=_event_order) if rows else None


def scope_refusal(conn: sqlite3.Connection, target: str, requester: Optional[str], episode: str,
                  comment_id: Any) -> Optional[str]:
    """None when ``requester`` may promote ``target`` for ``episode``; works on a replica or the home's live file."""
    from hermes_cli import kanban_spend_decision as ksd
    from hermes_cli.kanban_continuation import HUMAN_BLOCK_KINDS
    from hermes_cli.kanban_operator_call import COMMENT_MARK
    from hermes_cli.kanban_spend_decision import execution_restricted as restricted
    if restricted(conn, target):
        return 'Provider safety restriction has no supported execution release; ordinary spend promotion is not clearance'
    row = conn.execute("SELECT status, human_request, block_kind FROM tasks WHERE id = ?", (target,)).fetchone()
    if row is None:
        return f"no such task {target}"
    if not requester or requester == target:
        return f"{ACTION} is made by the stop's decision card, never by the stopped card itself"
    if row[0] != "triage":
        return f"{target} is {row[0]}, not held in triage by the spend budget"
    if row[1] or row[2] in HUMAN_BLOCK_KINDS:
        return f"{target} waits on a human ({row[2] or 'human_request'}); only the human releases it"
    if not ksd.pending(conn, target):
        return f"{target} has no pending spend-budget decision"
    stop = _current_stop(conn, target)
    current = (stop.get("uid") or str(stop["id"])) if stop else None
    if stop is None or current != episode:
        return f"episode {episode} is not {target}'s current spend stop ({current}); reread the card"
    producer = None
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?", (target, ksd.NOTICE)):
        p = _payload(raw)
        if p.get("episode") == episode and isinstance(p.get("producer"), dict):
            producer = p["producer"]
    if producer is None:
        return f"spend stop {episode} on {target} has no decision producer receipt yet"
    if producer.get("kind") != "supervisor" or producer.get("task_id") != requester:
        return (f"spend stop {episode} on {target} names its decision owner as {producer.get('kind')} "
                f"{producer.get('task_id') or ''}".rstrip() + f", not {requester}")
    from hermes_cli.kanban_sync_divergence import _event_order
    cid, after = _pos_int(comment_id), _event_order(stop)
    decided = None
    for ev in conn.execute("SELECT * FROM task_events WHERE task_id = ? AND kind = ?", (target, COMMENT_MARK)):
        ev = dict(ev)
        p = _payload(ev["payload"])
        if (cid is not None and _pos_int(p.get("comment_id")) == cid
                and (p.get("operator_caller") or {}).get("task_id") == requester
                and _event_order(ev) > after):
            decided = ev
            break
    if decided is None:
        return (f"decision comment {comment_id} is not {requester}'s operator comment on {target} made after "
                f"spend stop {episode}; record the decision there first")
    # A human who spoke on the card after the decision was recorded has not been heard by it: the decision card
    # reads that comment and records a new decision (the manual promote's unread-comment gate, for this path).
    decided_at = _event_order(decided)
    for ev in conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? AND kind = 'commented' AND json_valid(payload) AND "
            "(json_extract(payload, '$.writer_kind') = 'human' "
            "OR json_extract(payload, '$.relayed_human') IS NOT NULL)",
            (target,)):
        if _event_order(dict(ev)) > decided_at:
            return (f"a human commented on {target} after decision comment {cid}; read it and record a new "
                    "decision comment first")
    return None


def apply(conn: sqlite3.Connection, payload: dict, extra: dict) -> tuple[str, Optional[str]]:
    """Home side: budget and promotion in one transaction, or nothing."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_spend_budget as ksb
    from hermes_cli.kanban_operator_call import _marker
    target, uid = payload["target_task_id"], payload["request_uid"]
    caller, args = payload.get("caller") or {}, payload.get("args") or {}
    requester = caller.get("task_id")
    if _marker(conn, target, "promoted_manual", uid):
        return "already_applied", None
    runs = _pos_int(args.get("runs"))
    if runs is None or not 1 <= runs <= MAX_RUNS:
        return "refused", f"runs must be 1..{MAX_RUNS}, not {args.get('runs')!r}"
    episode, cid = str(args.get("episode") or ""), _pos_int(args.get("decision_comment_id"))
    reason = str(args.get("reason") or "").strip()
    if not reason:
        return "refused", "empty reason"
    actor = f"{caller.get('gateway')}/{requester}"
    with kb.write_txn(conn):
        why = scope_refusal(conn, target, requester, episode, cid)
        if why:
            return "refused", why
        open_parents = [pid for pid, st in kb.parent_states(conn, target) if st not in ("done", "archived")]
        if open_parents:
            return "refused", f"unsatisfied parent dependencies: {', '.join(open_parents)}"
        prior = ksb.card_override(conn, target) or {}
        upd = conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'triage'", (target,))
        if upd.rowcount != 1:
            raise RuntimeError(f"{target} left triage inside the promotion transaction")
        budget = {"runs": runs, "cache_read_tokens": ksb._limit_value(prior.get("cache_read_tokens")),
                  "by": actor, "reason": reason, "episode": episode, **extra}
        kb._append_event(conn, target, ksb.OVERRIDE_EVENT, budget)
        kb._append_event(conn, target, "promoted_manual", {
            "actor": actor, "reason": reason, "from_status": "triage", **extra,
            WINDOW_KEY: {"runs": runs, "episode": episode, "decision_comment_id": cid}})
        kb.add_comment(conn, target, ksb.AUTHOR,
                       f"Spend decision applied by {actor}: promoted with a new window of exactly {runs} run(s) "
                       f"(decision comment {cid}, stop {episode}): {reason}", system=True)
    return "applied", f"promoted with a {runs}-run spend window"
