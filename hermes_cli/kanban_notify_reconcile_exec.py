"""Non-worker executor for ``reconcile_notify`` supervisor requests. [daveey]

``kanban_notify_reconcile.reconcile_refused_sub`` retires a wrong-local ``platform='tui'``
``send_refused`` subscription for a chat another gateway owns, but it mutates the board, and every
Kanban worker process is write-fenced (``kanban_db_fence``). A worker therefore files a
``kanban_supervisor_request`` op ``{"op": "reconcile_notify", "task_id", "chat_id", "thread_id"?}`` and
the dispatcher (or ``hermes kanban supervisor-requests --drain``) runs it here. The request names the row
only: no evidence is accepted from the worker.

Evidence is read from the owner gateway itself, through a hermes-hub request (client >= 1.9.24):
``sessions/<profile>/<chat>`` action ``notice-receipts`` with the exact pending
``remote:<self gw>:<profile>/<board>/<task>:<event>`` keys. The owner answers from its own session store
(core's lineage-aware ``SessionDB.kanban_notice_keys_delivered``); the courier only lets the request's
``to_gw`` post the result, and the result lands in this host's hub.db ``sent`` row.

Two phases, so the dispatcher tick never blocks on another machine:

1. :func:`start` (after acceptance) picks the candidate owner from the task's existing ``platform='hub'``
   subscription (``<gw>/<profile>/<chat>``, exactly one other gateway), mints one read id per
   (gateway, profile, chat), stores them in the request's ``result_json`` (phase ``awaiting_owner``) and
   queues the reads. The request stays ``accepted``.
2. :func:`poll` (every drain) takes waiting requests by compare-and-swap, without spending a delivery
   attempt, and reads the ``sent`` rows. ``done`` with a matching envelope and echo is the only evidence;
   it backs the readers of the unchanged ``reconcile_refused_sub``. Any other owner outcome, a missing
   answer past :data:`OWNER_DEADLINE_S`, or a mismatch keeps the row (retryable by a new request).

Never sends, wakes a chat, or moves a cursor. The requester gets the usual ``supervisor_request_*`` event
and comment through ``kanban_supervisor_requests._finish``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

OP = "reconcile_notify"
PHASE = "awaiting_owner"
ACTION = "notice-receipts"
OWNER_DEADLINE_S = 24 * 3600  # a sleeping laptop answers late; the courier keeps a request 7 days
MAX_KEYS = 50  # the owner's per-request limit (hermes-hub notice_keys)
HUB_TIMEOUT_S = 30
_FIELDS = {"op", "task_id", "chat_id", "thread_id"}
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_FAILED = ("rejected", "expired", "failed", "quarantined")


# --- request shape ---------------------------------------------------------------------------------

def normalize_op(i: int, op: dict) -> dict:
    from hermes_cli.kanban_supervisor_requests import SupervisorRequestError
    extra = set(op) - _FIELDS
    if extra:
        raise SupervisorRequestError("invalid_ops", f"op {i}: {OP} takes only task_id, chat_id, thread_id "
                                     f"(no evidence fields; the executor reads the owner itself): {sorted(extra)}")
    task, chat = str(op.get("task_id") or "").strip(), str(op.get("chat_id") or "").strip()
    thread = str(op.get("thread_id") or "").strip()
    if not task or not _NAME.fullmatch(chat):
        raise SupervisorRequestError("invalid_ops", f"op {i}: {OP} needs task_id and a plain chat_id")
    return {"op": OP, "task_id": task, "chat_id": chat, "thread_id": thread}


def label(op: dict) -> str:
    if op.get("op") == OP:
        return f"{OP} {op['task_id']} tui:{op['chat_id']}" + (f"#{op['thread_id']}" if op.get("thread_id") else "")
    return f"{op['op']} {op['parent_id']} -> {op['child_id']}"


def _tui_row(conn, op):
    return conn.execute(
        "SELECT * FROM kanban_notify_subs WHERE task_id=? AND platform='tui' AND chat_id=? AND thread_id=?",
        (op["task_id"], op["chat_id"], op["thread_id"])).fetchone()


def preflight_op(conn: sqlite3.Connection, op: dict) -> dict:
    """``supervisor`` for a send_refused tui row, ``noop`` when the row is gone, else ``refused``."""
    from hermes_cli import kanban_db_notify as notify
    verdict = dict(op)
    if conn.execute("SELECT 1 FROM tasks WHERE id=?", (op["task_id"],)).fetchone() is None:
        return {**verdict, "verdict": "refused", "reason": f"unknown task {op['task_id']}"}
    row = _tui_row(conn, op)
    if row is None:
        return {**verdict, "verdict": "noop", "reason": "no tui subscription for that chat"}
    reason = notify._decode_notify_delivery_metadata(row["delivery_metadata"]).get("reason")
    if reason != "send_refused":
        return {**verdict, "verdict": "refused", "reason": f"only send_refused rows are reconciled (this one: {reason})"}
    return {**verdict, "verdict": "supervisor"}


# --- local identity and readers --------------------------------------------------------------------

def publisher(conn: sqlite3.Connection) -> tuple[str, str]:
    """(profile, board) under which hermes-hub publishes this board's tasks, from the db file path."""
    from hermes_constants import get_default_hermes_root
    row = next((r for r in conn.execute("PRAGMA database_list") if r[1] == "main"), None)
    if not row or not row[2]:
        raise ValueError("board database has no file")
    path, root = Path(row[2]).resolve(), get_default_hermes_root().resolve()
    homes = {root: "default"}
    if (root / "profiles").is_dir():
        homes.update({p.resolve(): p.name for p in (root / "profiles").iterdir() if p.is_dir()})
    if path.name == "kanban.db" and path.parent in homes:
        return homes[path.parent], "default"
    if path.name == "kanban.db" and path.parent.parent.name == "boards" and path.parents[3] in homes \
            and path.parents[2].name == "kanban":
        return homes[path.parents[3]], path.parent.name
    raise ValueError(f"board {path} is not under a hermes-hub published home ({root})")


def local_session_exists(_profile: str, chat: str) -> bool:
    """Whether any profile's session store on THIS host has ``chat``. An unreadable store raises."""
    from hermes_constants import get_default_hermes_root
    root = get_default_hermes_root()
    stores = [root / "state.db"]
    if (root / "profiles").is_dir():
        stores += sorted(p / "state.db" for p in (root / "profiles").iterdir() if p.is_dir())
    for store in stores:
        if not store.exists():
            continue
        conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=10)
        try:
            if conn.execute("SELECT 1 FROM sessions WHERE id=?", (chat,)).fetchone():
                return True
        finally:
            conn.close()
    return False


def _hub_candidates(conn, op, self_gw) -> list[tuple[str, str]]:
    """(gateway, profile) of the task's hub subscriptions for this chat on another gateway."""
    out = []
    for (chat_id,) in conn.execute(
            "SELECT chat_id FROM kanban_notify_subs WHERE task_id=? AND platform='hub' AND thread_id=?",
            (op["task_id"], op["thread_id"])):
        parts = str(chat_id).split("/")
        if len(parts) == 3 and parts[2] == op["chat_id"] and parts[0] != self_gw and all(map(_NAME.fullmatch, parts)):
            out.append((parts[0], parts[1]))
    return sorted(set(out))


# --- hermes-hub boundary ---------------------------------------------------------------------------

def _hub_bin() -> str:
    from hermes_cli.kanban_capability_registry import hub_cli_path
    return hub_cli_path()


def _run_hub(argv: list[str]) -> tuple[int, str, str]:
    """The hermes-hub CLI (argv boundary; tests replace this)."""
    try:
        p = subprocess.run([_hub_bin(), *argv], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=HUB_TIMEOUT_S, check=False)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


def _queue(read: dict) -> Optional[str]:
    """Queue (idempotently: same id, same envelope) one owner read. Returns an error that ends the request."""
    rc, out, err = _run_hub(["request", read["to_gw"], "sessions", f"{read['profile']}/{read['chat']}", ACTION,
                             "--arg", "keys=" + ",".join(read["keys"]), "--id", read["id"]])
    if rc == 0:
        return None
    text = (err or out or f"exit {rc}").strip()[:300]
    if "request_id_conflict" in text or "unknown_gateway" in text:
        return f"owner read {read['id']} could not be queued: {text}"
    _log.warning("notify reconcile: queueing owner read %s failed (%s); retrying next drain", read["id"], text)
    return None


def _sent(read_id: str) -> Optional[dict]:
    from hermes_cli.kanban_capability_registry import hub_db_path
    path = hub_db_path()
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        row = conn.execute("SELECT body, state, result FROM sent WHERE id=?", (read_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"body": json.loads(row[0] or "{}"), "state": row[1],
            "result": json.loads(row[2]) if row[2] else None}


def _evidence(read: dict, sent: dict) -> dict:
    """The owner's answer, only if the stored envelope and the result's echo match this read exactly."""
    body, res = sent["body"], sent["result"]
    want = {"id": read["id"], "to_gw": read["to_gw"], "coll": "sessions", "key": f"{read['profile']}/{read['chat']}",
            "action": ACTION, "args": {"keys": ",".join(read["keys"])}}
    if any(body.get(k) != v for k, v in want.items()):
        raise ValueError(f"owner read {read['id']}: stored envelope does not match")
    if not isinstance(res, dict) or res.get("code") != "notice_receipts" or res.get("profile") != read["profile"] \
            or res.get("session") != read["chat"] or res.get("keys") != read["keys"] \
            or type(res.get("session_exists")) is not bool or not isinstance(res.get("delivered"), list) \
            or not set(res["delivered"]) <= set(read["keys"]):
        raise ValueError(f"owner read {read['id']}: answer does not echo the request")
    return res


# --- phases ----------------------------------------------------------------------------------------

def _save(conn, req, executor, state: dict, now: int) -> bool:
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE supervisor_requests SET result_json=?, lease_expires=? WHERE id=? AND status='accepted' AND executor=?",
            (json.dumps(state), now + _lease(), req["id"], executor))
    return cur.rowcount == 1


def _lease() -> int:
    from hermes_cli.kanban_supervisor_requests import LEASE_SECONDS
    return LEASE_SECONDS


def _reconcile(conn, op, ident, *, owner_ans: Optional[dict] = None, read: Optional[dict] = None) -> str:
    from hermes_cli.kanban_notify_reconcile import reconcile_refused_sub

    def owner(profile, chat):
        if owner_ans is None or read is None or chat != read["chat"] or profile != read["profile"] or not owner_ans["session_exists"]:
            return None
        return read["to_gw"], profile

    def delivered(gw, profile, chat, keys):
        if read is None or owner_ans is None or (gw, profile, chat) != (read["to_gw"], read["profile"], read["chat"]) \
                or not set(keys) <= set(read["keys"]):
            raise ValueError("pending notices changed since the owner was asked")
        return set(owner_ans["delivered"]) & set(keys)

    return reconcile_refused_sub(conn, task_id=op["task_id"], chat_id=op["chat_id"], thread_id=op["thread_id"],
                                 source_gateway=ident["gateway"], source_profile=ident["profile"],
                                 board=ident["board"], local_session_exists=local_session_exists,
                                 owner=owner, delivered_keys=delivered)


def start(conn: sqlite3.Connection, req: dict, executor: str, *, now: Optional[int] = None) -> str:
    """Phase 1. Returns the request status (``awaiting_owner`` while it waits)."""
    from hermes_cli.kanban_capability_registry import self_gateway
    from hermes_cli.kanban_notify_reconcile import _pending
    now = int(time.time()) if now is None else now
    ops = req["ops"]
    gw = self_gateway()
    try:
        profile, board = publisher(conn)
    except ValueError as exc:
        return _end(conn, req, executor, "failed", [{**o, "outcome": "not_applied"} for o in ops], str(exc))
    if not gw:
        return _end(conn, req, executor, "failed", [{**o, "outcome": "not_applied"} for o in ops],
                    "this host has no hermes-hub gateway identity")
    ident = {"gateway": gw, "profile": profile, "board": board}
    results: list[Optional[dict]] = [None] * len(ops)
    reads: dict[tuple, dict] = {}
    for i, op in enumerate(ops):
        cands = _hub_candidates(conn, op, gw)
        row = _tui_row(conn, op)
        if row is None or len(cands) != 1:
            # No (or no single) remote route: reconcile_refused_sub gives the canonical outcome without
            # any owner evidence (absent, provenance_mismatch, local_owner, unknown_owner, ...).
            results[i] = {**op, "outcome": _reconcile(conn, op, ident)}
            if len(cands) > 1:
                results[i]["detail"] = "hub routes on several gateways: " + ", ".join(g for g, _ in cands)
            continue
        to_gw, owner_profile = cands[0]
        if owner_profile != (row["notifier_profile"] or "default"):
            results[i] = {**op, "outcome": "unknown_owner", "detail": "hub route names another profile"}
            continue
        events = _pending(conn, op["task_id"], row["last_event_id"])
        if not events or any(kind != "completed" for _, kind in events):
            # Same outcomes reconcile_refused_sub gives; no receipt can justify these, so the owner is not asked.
            results[i] = {**op, "outcome": "unsupported_events" if events else "nothing_pending"}
            continue
        keys = [f"remote:{gw}:{profile}/{board}/{op['task_id']}:{eid}" for eid, _ in events]
        read = reads.setdefault((to_gw, owner_profile, op["chat_id"]), {
            "id": f"nr-{gw}-{req['id']}-{secrets.token_hex(6)}-{len(reads)}", "to_gw": to_gw,
            "profile": owner_profile, "chat": op["chat_id"], "keys": [], "ops": []})
        read["keys"] = sorted(set(read["keys"]) | set(keys))
        read["ops"].append(i)
    if any(len(r["keys"]) > MAX_KEYS for r in reads.values()):
        return _end(conn, req, executor, "rejected", [{**o, "outcome": "not_applied"} for o in ops],
                    f"more than {MAX_KEYS} pending notices for one chat; split the request")
    if not reads:
        return _end(conn, req, executor, "done", results, None)
    state = {"phase": PHASE, "deadline": now + OWNER_DEADLINE_S, "ident": ident,
             "reads": list(reads.values()), "results": results}
    if not _save(conn, req, executor, state, now):
        return "lost_lease"
    for read in state["reads"]:
        error = _queue(read)
        if error:
            return _end(conn, req, executor, "failed", _kept(ops, results, "evidence_unavailable"), error)
    return PHASE


def _kept(ops, results, outcome, detail=None):
    return [r or {**o, "outcome": outcome, **({"detail": detail} if detail else {})} for o, r in zip(ops, results)]


def _end(conn, req, executor, status, results, error) -> str:
    from hermes_cli.kanban_supervisor_requests import _finish
    return status if _finish(conn, req, executor, status, results=results, error=error) else "lost_lease"


def _take(conn, rid: int, holder: Optional[str], executor: str, now: int) -> Optional[dict]:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_supervisor_requests import _row
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE supervisor_requests SET executor=?, lease_expires=? WHERE id=? AND status='accepted' "
            "AND COALESCE(executor,'')=?", (executor, now + _lease(), rid, holder or ""))
        if cur.rowcount != 1:
            return None
        return _row(conn.execute("SELECT * FROM supervisor_requests WHERE id=?", (rid,)).fetchone())


def awaiting(conn: sqlite3.Connection) -> list:
    return conn.execute(
        "SELECT id, executor, lease_expires FROM supervisor_requests WHERE status='accepted' AND result_json IS NOT NULL "
        "AND json_valid(result_json) AND json_extract(result_json, '$.phase')=? ORDER BY id", (PHASE,)).fetchall()


def poll(conn: sqlite3.Connection, executor: str, *, now: Optional[int] = None) -> list[tuple[int, str]]:
    """Phase 2 over every waiting request. Does not spend delivery attempts."""
    from hermes_cli.kanban_supervisor_requests import (SupervisorRequestError, _executor_dead,
                                                       _non_worker_refusal)
    refusal = _non_worker_refusal()
    if refusal:
        raise SupervisorRequestError("worker_context", refusal)
    now = int(time.time()) if now is None else now
    processed = []
    for rid, holder, lease in awaiting(conn):
        if holder != executor and lease and int(lease) > now and not _executor_dead(holder):
            continue  # another live executor polls it
        req = _take(conn, int(rid), holder, executor, now)
        if req is None:
            continue
        try:
            status = _advance(conn, req, executor, now)
        except Exception as exc:  # health: allow BLE001 -- recorded on the request; the row is kept
            _log.exception("notify reconcile request %s failed", rid)
            from hermes_cli.kanban_supervisor_requests import _row
            fresh = _row(conn.execute("SELECT * FROM supervisor_requests WHERE id=?", (int(rid),)).fetchone())
            saved = (fresh.get("result") or {}).get("results") or [None] * len(req["ops"])  # incl. reads settled earlier
            status = _end(conn, req, executor, "failed", _kept(req["ops"], saved, "evidence_unavailable"),
                          f"{type(exc).__name__}: {exc}"[:500])
        if status != PHASE:
            processed.append((int(rid), status))
    return processed


def _advance(conn, req, executor, now) -> str:
    state = req["result"]
    ops, results, ident = req["ops"], list(state["results"]), state["ident"]
    waiting = []
    for read in state["reads"]:
        if all(results[i] is not None for i in read["ops"]):
            continue  # settled on an earlier poll; re-running it would relabel a retired row "absent"
        try:
            sent = _sent(read["id"])
        except (sqlite3.Error, ValueError) as exc:
            _log.warning("notify reconcile: reading owner read %s failed (%s); retrying next drain", read["id"], exc)
            waiting.append(read)
            continue
        if sent is None:
            error = _queue(read)  # never queued (hub CLI was down): same id, same envelope
            if error:
                for i in read["ops"]:
                    results[i] = {**ops[i], "outcome": "evidence_unavailable", "detail": error}
                continue
            waiting.append(read)
        elif sent["state"] == "done":
            try:
                ans = _evidence(read, sent)
            except ValueError as exc:
                for i in read["ops"]:
                    results[i] = {**ops[i], "outcome": "evidence_unavailable", "detail": str(exc)}
                continue
            for i in read["ops"]:
                results[i] = {**ops[i], "outcome": _reconcile(conn, ops[i], ident, owner_ans=ans, read=read),
                              "owner": read["to_gw"]}
            if not _save(conn, req, executor, {**state, "results": results}, now):
                return "lost_lease"  # rows this read retired stay retired; the report is the new holder's
        elif sent["state"] in _FAILED:
            err = (sent["result"] or {}).get("code") or (sent["result"] or {}).get("error") or sent["state"]
            for i in read["ops"]:
                results[i] = {**ops[i], "outcome": "evidence_unavailable",
                              "detail": f"owner {read['to_gw']} read {sent['state']}: {err}"[:300]}
        else:
            waiting.append(read)
    if waiting and now < int(state["deadline"]):
        state = {**state, "results": results}
        return PHASE if _save(conn, req, executor, state, now) else "lost_lease"
    if waiting:
        names = ", ".join(sorted({r["to_gw"] for r in waiting}))
        return _end(conn, req, executor, "failed", _kept(ops, results, "evidence_unavailable",
                                                         f"owner {names} did not answer"),
                    f"owner {names} did not answer within {OWNER_DEADLINE_S // 3600} h; rows kept")
    return _end(conn, req, executor, "done", results, None)
