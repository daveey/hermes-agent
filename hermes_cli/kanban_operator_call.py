"""Scoped Board-operator call: a worker asks for ONE op on ONE named foreign card. [daveey]

A gateway's live ``kanban.db`` holds only its own cards, so ``kanban_comment`` / ``kanban_move`` / ``kanban_unblock``
on a card another gateway created refuse ``foreign_card``. This module is the supported path, one
``kanban_supervisor_request`` op (``{"op": "operator", "action": "comment"|"move"|"unblock", "task_id": ...}``):

1. Request side (the caller's gateway, its non-worker supervisor drain): re-checks scope, appends
   ``operator_call_requested`` on the caller's OWN card (the Board carries it to every replica) and parks the request
   in phase ``awaiting_home``.
2. Home side (every gateway's dispatcher tick, :func:`apply_incoming`): reads new request events from its replica
   (id watermark), keeps those naming a card this gateway created, proves the caller (the run was live, the Board
   lease holder and the event's Board ``origin_gateway`` are the caller's gateway), applies the op through the
   ordinary functions on its live file and appends ``operator_call_result`` on the target card.
3. Request side again (:func:`poll`): finds that result in the replica, finishes the request and wakes the caller.

Card actions are comment, move, unblock, creator_archive (exact creator scope) and spend_promote (only the spend
stop's named decision card, a finite run window: ``kanban_spend_promote``), plus ``label_publish`` (the
hub executes it with its Board admin key, after re-running its own probe of that machine, and answers on the
requester's card). complete / archive / link / unlink / block / edit of a foreign card stay with its owner and are
refused; so is any second op or a card the op does not name. No Board admin key is read here.
Every call, accepted or refused, is logged in ``operator_calls`` on the gateway that saw it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional

_log = logging.getLogger(__name__)

OP = "operator"
CARD_ACTIONS = ("comment", "move", "unblock", "creator_archive", "spend_promote", "protect_stopped")
LABEL_PUBLISH = "label_publish"  # names a machine, not a card (kanban_operator_labels)
ACTIONS = (*CARD_ACTIONS, LABEL_PUBLISH)
OWNER_ONLY = ("complete", "archive", "link", "unlink", "block", "edit", "assign", "delete", "reclaim")
REQUEST_KIND = "operator_call_requested"
RESULT_KIND = "operator_call_result"
REFUSED_KIND = "operator_call_refused"
COMMENT_MARK = "operator_call_commented"
MOVE_MARK = "operator_call_moved"
PHASE = "awaiting_home"
DEADLINE_S = 24 * 3600
MAX_TEXT = 8000
MAX_REASON = 2000
MAX_IN_FLIGHT = 3
MAX_PER_DAY = 20
MAX_PER_TICK = 5
SCAN_BATCH = 500
SKEW_S = 120
AUTHOR = "kanban-operator-call"
_FIELDS = {"comment": {"text"}, "move": {"runs_on", "reason", "host_reason"}, "unblock": {"reason"},
           "creator_archive": {"reason"}}
_MARK: dict = {}  # (live db path, board) -> replica event id scanned up to, this process
_PERSISTED: dict = {}

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS operator_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_uid TEXT NOT NULL,
        side TEXT NOT NULL,
        board TEXT,
        action TEXT,
        target_task_id TEXT,
        caller_gateway TEXT,
        caller_task_id TEXT,
        caller_run_id INTEGER,
        caller_run_uid TEXT,
        caller_profile TEXT,
        caller_host TEXT,
        executor_gateway TEXT,
        args_digest TEXT,
        request_event_uid TEXT,
        outcome TEXT NOT NULL,
        detail TEXT,
        supervisor_request_id INTEGER,
        created_at INTEGER NOT NULL,
        finished_at INTEGER,
        UNIQUE(request_uid, side))""",
    "CREATE INDEX IF NOT EXISTS idx_operator_calls_caller ON operator_calls(caller_task_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_operator_calls_target ON operator_calls(target_task_id, created_at)",
    "CREATE TABLE IF NOT EXISTS operator_call_scan (board TEXT PRIMARY KEY, last_id INTEGER NOT NULL, "
    "last_uid TEXT)",
)


def _err(code: str, message: str):
    from hermes_cli.kanban_supervisor_requests import SupervisorRequestError
    return SupervisorRequestError(code, message)


def ensure_schema(conn: sqlite3.Connection) -> None:
    from hermes_cli import kanban_operator_labels as kol
    for stmt in (*_SCHEMA, *kol.SCHEMA):
        conn.execute(stmt)
    if "target_machine" not in {r[1] for r in conn.execute("PRAGMA table_info(operator_calls)")}:
        conn.execute("ALTER TABLE operator_calls ADD COLUMN target_machine TEXT")


def _has_log(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operator_calls'").fetchone() is not None


# --- validation ----------------------------------------------------------------------------------

def is_operator(raw_ops: Any) -> bool:
    return isinstance(raw_ops, list) and any(isinstance(o, dict) and str(o.get("op") or "").strip() == OP
                                             for o in raw_ops)


def normalize_op(i: int, op: dict) -> dict:
    action = str(op.get("action") or "").strip()
    if action == "protect_stopped":
        from hermes_cli import kanban_protect_operator as protection
        return protection.normalize(i, op)
    if action in OWNER_ONLY:
        raise _err("operator_action_refused", f"op {i}: {action} of another gateway's card stays with its owner; "
                   f"the operator call only does {', '.join(ACTIONS)}")
    if action == LABEL_PUBLISH:
        from hermes_cli import kanban_operator_labels as kol
        return kol.normalize(i, op)
    if action == "spend_promote":
        from hermes_cli import kanban_spend_promote as ksp
        return ksp.normalize(i, op)
    if action not in ACTIONS:
        raise _err("operator_action_refused", f"op {i}: action must be one of {', '.join(ACTIONS)}")
    task_id = str(op.get("task_id") or "").strip()
    if not task_id:
        raise _err("invalid_ops", f"op {i}: task_id (the one foreign card) is required")
    extra = set(op) - {"op", "action", "task_id"} - _FIELDS[action]
    if extra:
        raise _err("invalid_ops", f"op {i}: {action} takes only task_id and {', '.join(sorted(_FIELDS[action]))}; "
                   f"unexpected {', '.join(sorted(extra))} (one call names one card)")
    out: dict[str, Any] = {"op": OP, "action": action, "task_id": task_id}
    if action == "comment":
        text = str(op.get("text") or "").strip()
        if not text:
            raise _err("invalid_ops", f"op {i}: comment text is required")
        if len(text) > MAX_TEXT:
            raise _err("invalid_ops", f"op {i}: comment text is over {MAX_TEXT} characters")
        out["text"] = text
    else:
        reason = str(op.get("reason") or "").strip()
        if not reason:
            raise _err("invalid_ops", f"op {i}: reason is required for {action}")
        out["reason"] = reason[:MAX_REASON]
    if action == "move":
        from hermes_cli import kanban_worker_node as wn
        try:
            labels = wn.normalize_runs_on(op.get("runs_on"))
        except ValueError as exc:
            raise _err("invalid_ops", f"op {i}: {exc}") from None
        out["runs_on"] = list(labels or [])
        # A machine:<x> target needs host_reason naming state on x (kanban_move_pool.check): optional, carried.
        host_reason = str(op.get("host_reason") or "").strip()
        if host_reason:
            out["host_reason"] = host_reason[:MAX_REASON]
    return out


def label(op: dict) -> str:
    if op.get("action") == LABEL_PUBLISH:
        return f"operator {LABEL_PUBLISH} {op.get('machine')} {','.join(op.get('labels') or [])}"
    return f"operator {op.get('action')} {op.get('task_id')}"


def _digest(op: dict) -> str:
    body = {k: v for k, v in op.items() if k not in ("op",)}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


# --- seams (tests replace these) -----------------------------------------------------------------

def _own_gateway(conn: sqlite3.Connection) -> Optional[str]:
    from hermes_cli import kanban_capability_routing as kcr
    return kcr._own_gateway(conn)


def _db_path(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("PRAGMA database_list").fetchone()
    return row[2] if row and row[2] else None


@contextmanager
def _replica(conn: sqlite3.Connection) -> Iterator[Optional[sqlite3.Connection]]:
    from hermes_cli import kanban_replica_reader
    with kanban_replica_reader.open_replica(_db_path(conn)) as rep:
        yield rep


def _board_slug(conn: sqlite3.Connection) -> str:
    from hermes_cli import kanban_db_sync as sync
    path = _db_path(conn)
    return sync.board_slug_for_path(path) if path else "default"


def _evidence(board: str, event_uid: str) -> dict:
    """The Board's own copy of the request event (raises when the Board is unreachable)."""
    from hermes_cli import kanban_sync_client as ksc
    return ksc.Hub(ksc.load_identity()).row_evidence(board, "task_events", event_uid)


def _outbox_rows(conn: sqlite3.Connection) -> Optional[int]:
    try:
        return conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE table_name = 'task_events'").fetchone()[0]
    except sqlite3.OperationalError:
        return None


def _synced(conn: sqlite3.Connection) -> bool:
    from hermes_cli import kanban_db_sync as sync
    try:
        return bool(sync.sync_state(conn))
    except Exception:  # health: allow BLE001 -- an unreadable sync state is an unsynced board
        return False


# --- log -----------------------------------------------------------------------------------------

def log(conn: sqlite3.Connection, *, request_uid: str, side: str, outcome: str, op: Optional[dict] = None,
        caller: Optional[dict] = None, detail: Optional[str] = None, board: Optional[str] = None,
        executor_gateway: Optional[str] = None, request_event_uid: Optional[str] = None,
        supervisor_request_id: Optional[int] = None, finished: bool = False) -> None:
    """Insert or update the ``(request_uid, side)`` log row inside the caller's txn."""
    ensure_schema(conn)
    op, caller, now = op or {}, caller or {}, int(time.time())
    conn.execute(
        "INSERT INTO operator_calls (request_uid, side, board, action, target_task_id, caller_gateway, "
        "caller_task_id, caller_run_id, caller_run_uid, caller_profile, caller_host, executor_gateway, args_digest, "
        "request_event_uid, outcome, detail, supervisor_request_id, created_at, finished_at, target_machine) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(request_uid, side) DO UPDATE SET "
        "outcome=excluded.outcome, detail=COALESCE(excluded.detail, detail), "
        "request_event_uid=COALESCE(excluded.request_event_uid, request_event_uid), "
        "executor_gateway=COALESCE(excluded.executor_gateway, executor_gateway), finished_at=excluded.finished_at",
        (request_uid, side, board, op.get("action"), op.get("task_id"), caller.get("gateway"),
         caller.get("task_id"), caller.get("run_id"), caller.get("run_uid"), caller.get("profile"),
         caller.get("host"), executor_gateway, _digest(op) if op else None, request_event_uid, outcome,
         (detail or None) and str(detail)[:500], supervisor_request_id, now, now if finished else None,
         op.get("machine")))


def _caller_here(conn: sqlite3.Connection, task: Optional[str], run: Optional[int]) -> dict:
    run_uid = None
    if task:
        try:
            row = conn.execute("SELECT uid FROM task_runs WHERE id = ?", (run,)).fetchone()
            run_uid = row[0] if row else None
        except sqlite3.OperationalError:
            run_uid = None
    return {"gateway": _own_gateway(conn), "task_id": task, "run_id": run, "run_uid": run_uid,
            "profile": os.environ.get("HERMES_PROFILE") or "default", "host": socket.gethostname()}


def log_submit_refusal(conn: sqlite3.Connection, *, env_task: Optional[str], env_run: Optional[str], raw_ops: Any,
                       code: str, message: str) -> None:
    """Record a refused operator call (its own txn, committed before the caller raises)."""
    from hermes_cli import kanban_db as kb
    try:
        run = int(env_run) if env_run else None
    except ValueError:
        run = None
    op = next((o for o in raw_ops if isinstance(o, dict) and o.get("op") == OP), {}) if isinstance(raw_ops, list) else {}
    op = {"action": str(op.get("action") or "")[:40], "task_id": str(op.get("task_id") or "")[:64],
          "machine": str(op.get("machine") or "")[:64] or None}
    try:
        with kb.write_txn(conn):
            caller = _caller_here(conn, env_task, run)
            log(conn, request_uid=uuid.uuid4().hex, side="request", outcome="refused", op=op, caller=caller,
                detail=f"{code}: {message}", board=_board_slug(conn), finished=True)
            if env_task and kb.get_task(conn, env_task) is not None:
                kb._append_event(conn, env_task, REFUSED_KIND, {**op, "code": code, "detail": message[:500]})
    except Exception:  # health: allow BLE001 -- the refusal itself still reaches the caller
        _log.warning("kanban: could not log a refused operator call", exc_info=True)


# --- request side --------------------------------------------------------------------------------

def _target_row(rep: Optional[sqlite3.Connection], task_id: str) -> Optional[sqlite3.Row]:
    if rep is None:
        return None
    try:
        return rep.execute("SELECT id, status, created_on FROM tasks WHERE id = ?", (task_id,)).fetchone()
    except sqlite3.OperationalError:
        return None


def _rate_refusal(conn: sqlite3.Connection, requester: str) -> Optional[str]:
    if not _has_log(conn):
        return None
    now = int(time.time())
    open_n = conn.execute("SELECT COUNT(*) FROM operator_calls WHERE side='request' AND caller_task_id=? "
                          "AND outcome='forwarded'", (requester,)).fetchone()[0]
    if open_n >= MAX_IN_FLIGHT:
        return f"{requester} already has {open_n} operator calls in flight (max {MAX_IN_FLIGHT}); wait for them"
    day_n = conn.execute("SELECT COUNT(*) FROM operator_calls WHERE side='request' AND caller_task_id=? "
                         "AND outcome != 'refused' AND created_at > ?", (requester, now - 86400)).fetchone()[0]
    if day_n >= MAX_PER_DAY:
        return f"{requester} made {day_n} operator calls in 24 h (max {MAX_PER_DAY})"
    return None


def _label_preflight(conn, requester, op):
    from hermes_cli import kanban_operator_labels as kol
    synced = _synced(conn)
    own = _own_gateway(conn) if synced else None
    return kol.preflight(conn, requester, op, own=own, synced=synced,
                         rate=_rate_refusal(conn, requester) if own else None)


def preflight_op(conn: sqlite3.Connection, requester: str, op: dict) -> dict:
    from hermes_cli import kanban_capability_routing as kcr
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_protect_operator as protection
    special = {"protect_stopped": protection.preflight, LABEL_PUBLISH: _label_preflight}.get(op.get("action"))
    if special is not None:
        return special(conn, requester, op)
    target = op["task_id"]
    verdict = {**op}

    def refuse(code: str, why: str) -> dict:
        verdict.update(verdict="refused", code=code, reason=why)
        return verdict

    if target == requester:
        return refuse("own_card", f"{target} is this worker's own card; use the ordinary kanban tools")
    if kb.get_task(conn, target) is not None and kcr.card_home(conn, target) is None:
        return refuse("not_foreign", f"{target} lives on this gateway; use kanban_comment / kanban_move / "
                                     "kanban_unblock directly")
    if not _synced(conn) or not _own_gateway(conn):
        return refuse("not_synced", "this board does not sync to the Board, so no other gateway can be asked")
    with _replica(conn) as rep:
        row = _target_row(rep, target)
        if row is None:
            return refuse("unknown_task", f"no card {target} in this gateway's Board replica")
        status, home = row["status"], row["created_on"]
        if op["action"] == "creator_archive":
            from hermes_cli.kanban_creator_archive import refusal
            why = refusal(rep, target, requester)
            if why:
                return refuse("archive_scope", why)
        spend_scope = None
        if op["action"] == "spend_promote" and rep is not None:
            from hermes_cli import kanban_spend_promote as ksp
            spend_scope = ksp.scope_refusal(rep, target, requester, op["episode"], op["decision_comment_id"])
        human = None
        if op["action"] == "unblock" and rep is not None:
            human = rep.execute("SELECT human_request, block_kind FROM tasks WHERE id = ?", (target,)).fetchone()
        from hermes_cli import kanban_pickup_stall_repin as stall
        alarm_scope = stall.operator_refusal(conn, rep, requester, op)  # None unless an alarm asks
    if not home:
        return refuse("no_home", f"{target} has no created_on; no gateway owns it")
    if home == _own_gateway(conn):
        return refuse("not_foreign", f"{target} was created on this gateway")
    if status in ("done", "archived") and op["action"] != "creator_archive":
        return refuse("terminal", f"{target} is {status}")
    if spend_scope:
        return refuse("spend_scope", spend_scope)
    if op["action"] == "move" and status == "running":
        return refuse("running", f"{target} is running; only its own run may move it (kanban_move)")
    if alarm_scope is not None:
        return refuse("pickup_stall_scope", f"a pickup-stall alarm moves only a card it lists, off the stalled "
                                            f"machine, to the pool queue (or one machine whose state a host_reason "
                                            f"names): {alarm_scope}")
    if op["action"] == "unblock":
        if status != "blocked":
            return refuse("not_blocked", f"{target} is {status}, not blocked")
        refusal = _human_gate(human[0] if human else None, human[1] if human else None, target)
        if refusal:
            return refuse("human_gate", refusal)
    rate = _rate_refusal(conn, requester)
    if rate:
        return refuse("rate_limited", rate)
    verdict.update(verdict="supervisor", home=home)
    return verdict


def _human_gate(human_request: Any, block_kind: Optional[str], target: str) -> Optional[str]:
    from hermes_cli.kanban_continuation import HUMAN_BLOCK_KINDS
    if human_request or block_kind in HUMAN_BLOCK_KINDS:
        return (f"{target} waits on a human ({block_kind or 'human_request'}); only the human's own unblock "
                "releases it")
    return None


def start(conn: sqlite3.Connection, req: dict, executor: str) -> str:
    """Request-side execution: forward the call to the target's home and wait in phase ``awaiting_home``."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_supervisor_requests import _finish, requester_refusal
    requester, op = req["requester_task_id"], req["ops"][0]
    gone = requester_refusal(conn, requester)
    verdict = preflight_op(conn, requester, op) if not gone else {"verdict": "refused", "reason": gone}
    uid = uuid.uuid4().hex
    caller = _caller_here(conn, requester, req.get("requester_run_id"))
    board = _board_slug(conn)
    if verdict["verdict"] != "supervisor":
        with kb.write_txn(conn):
            log(conn, request_uid=uid, side="request", outcome="refused", op=op, caller=caller, board=board,
                detail=verdict.get("reason"), supervisor_request_id=req["id"], finished=True)
        _finish(conn, req, executor, "rejected", results=[{**op, "outcome": "not_applied",
                                                           "detail": verdict.get("reason")}],
                error=verdict.get("reason"))
        return "rejected"
    payload = {"request_uid": uid, "board": board, "action": op["action"], "target_task_id": op.get("task_id"),
               "args": {k: v for k, v in op.items() if k not in ("op", "action", "task_id")},
               # The call was made at submit (which proved the run live), not when this tick forwards it:
               # the caller may have completed since.
               "caller": caller, "requested_at": int(req["created_at"]),
               "supervisor_request_id": req["id"],
               "home": verdict.get("home")}
    if op['action'] == 'protect_stopped':
        payload['version'] = 1
    state = {"phase": PHASE, "request_uid": uid, "home": verdict.get("home"),
             "deadline": int(time.time()) + DEADLINE_S}
    with kb.write_txn(conn):
        before = _outbox_rows(conn)
        kb._append_event(conn, requester, REQUEST_KIND, payload,
                         run_id=req.get('requester_run_id') if op['action'] == 'protect_stopped' else None)
        if before is None or (_outbox_rows(conn) or 0) <= before:
            conn.execute("DELETE FROM task_events WHERE id = (SELECT MAX(id) FROM task_events WHERE task_id = ? "
                         "AND kind = ?)", (requester, REQUEST_KIND))
            log(conn, request_uid=uid, side="request", outcome="refused", op=op, caller=caller, board=board,
                detail="not_synced: the request event would not reach the Board", supervisor_request_id=req["id"],
                finished=True)
            failed = True
        else:
            ev = conn.execute("SELECT uid FROM task_events WHERE id = (SELECT MAX(id) FROM task_events WHERE "
                              "task_id = ? AND kind = ?)", (requester, REQUEST_KIND)).fetchone()
            log(conn, request_uid=uid, side="request", outcome="forwarded", op=op, caller=caller, board=board,
                request_event_uid=ev[0] if ev else None, supervisor_request_id=req["id"], executor_gateway=verdict.get('home'))
            cur = conn.execute("UPDATE supervisor_requests SET result_json=? WHERE id=? AND status='accepted' "
                               "AND executor=?", (json.dumps(state), req["id"], executor))
            failed = False
    if failed:
        _finish(conn, req, executor, "rejected", results=[{**op, "outcome": "not_applied", "detail": "not_synced"}],
                error="not_synced: this board cannot carry the request to the card's home")
        return "rejected"
    return PHASE if cur.rowcount == 1 else "lost_lease"


def awaiting(conn: sqlite3.Connection) -> list:
    return conn.execute(
        "SELECT id, executor, lease_expires FROM supervisor_requests WHERE status='accepted' AND result_json IS NOT "
        "NULL AND json_valid(result_json) AND json_extract(result_json, '$.phase')=? ORDER BY id", (PHASE,)).fetchall()


_RETRY = object()  # the Board could not vouch for a result yet: look again next tick


def _result_for(conn: sqlite3.Connection, target: str, uid: str, home: Optional[str], board: str) -> Any:
    """The home's answer to request ``uid``, else None or :data:`_RETRY`.

    ``request_uid`` is public (it rides on the caller's card), so a result counts only when it names the home as
    executor AND the Board says the home gateway wrote that event (``origin_gateway``). Any other copy is ignored
    and logged, so a forged result neither finishes the call nor hides the real answer.
    """
    def candidates(c):
        if c is None:
            return []
        out = []
        for euid, raw in c.execute("SELECT uid, payload FROM task_events WHERE task_id = ? AND kind = ? "
                                   "ORDER BY id DESC", (target, RESULT_KIND)).fetchall():
            try:
                p = json.loads(raw or "{}")
            except ValueError:
                continue
            if p.get("request_uid") == uid:
                out.append((euid, p))
        return out
    found = candidates(conn)  # a pickup copy of the target on this gateway gets the event from the feed
    with _replica(conn) as rep:
        found += candidates(rep)
    seen, retry = set(), False
    for euid, p in found:
        if not euid or euid in seen:
            continue
        seen.add(euid)
        if not home or p.get("executor_gateway") != home:
            _log.warning("kanban: ignoring operator_call_result %s for %s: executor %r is not the home %r",
                         euid, uid, p.get("executor_gateway"), home)
            continue
        try:
            ev = _evidence(board, euid)
        except Exception as exc:  # health: allow BLE001 -- unreachable Board: look again next tick
            _log.info("kanban: operator call result evidence for %s unavailable (%s); retrying", euid, exc)
            retry = True
            continue
        from hermes_cli.kanban_operator_delivery import canonical_payload
        try:
            canonical = canonical_payload(ev, euid, target, RESULT_KIND, home)
        except ValueError:
            _log.warning("kanban: ignoring operator_call_result %s: invalid Board identity", euid)
            continue
        if canonical != p:
            _log.warning("kanban: ignoring operator_call_result %s: payload differs from Board", euid)
            continue
        return {**canonical, "result_event_uid": euid}
    return _RETRY if retry else None


# `done` settles courier administration, not the target scope or its current hold.
_STATUS = {"applied": "done", "already_applied": "done", "recorded": "done",
           "refused": "rejected", "failed": "failed"}


def poll(conn: sqlite3.Connection, executor: str, *, now: Optional[int] = None) -> list[tuple[int, str]]:
    """Finish forwarded calls whose home answered (or that ran out of time). No delivery attempts spent."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_notify_reconcile_exec as nrx
    from hermes_cli.kanban_supervisor_requests import _executor_dead, _finish
    now = int(time.time()) if now is None else now
    processed = []
    for rid, holder, lease in awaiting(conn):
        if holder != executor and lease and int(lease) > now and not _executor_dead(holder):
            continue
        req = nrx._take(conn, int(rid), holder, executor, now)
        if req is None:
            continue
        state, op = req["result"], req["ops"][0]
        # A card action is answered on its target card; label_publish (a machine) on the requester's own card.
        answered_on = op.get("task_id") or req["requester_task_id"]
        res = _result_for(conn, answered_on, state["request_uid"], state.get("home"), _board_slug(conn))
        if res is _RETRY:
            res = None
        if res is None and (op.get("action") == "protect_stopped" or now < int(state.get("deadline") or 0)):
            continue
        if res is None:
            status, outcome, detail = "failed", "failed", (f"{state.get('home') or 'the home gateway'} did not "
                                                           f"answer within {DEADLINE_S // 3600} h")
        else:
            outcome, detail = str(res.get("outcome") or "failed"), res.get("detail")
            status = _STATUS.get(outcome, "failed")
        results = [{**op, "outcome": outcome, **({"detail": detail} if detail else {}),
                    "home": res.get("executor_gateway") if res else state.get("home")}]
        if res and op.get("action") == "protect_stopped":
            results[0]["protection"] = res.get("protection")
        if status == "done" and res and res.get("operator_delivery"):
            results[0]["operator_delivery"] = {**res["operator_delivery"],
                                               "result_event_uid": res["result_event_uid"]}
        with kb.write_txn(conn):
            log(conn, request_uid=state["request_uid"], side="request",
                outcome={"done": "done", "rejected": "rejected"}.get(status, "failed"), detail=detail, finished=True)
        if kb.get_task(conn, req["requester_task_id"]) is None:
            # A worker node evicted its copy of the caller card: the result stays on the target card.
            with kb.write_txn(conn):
                conn.execute("UPDATE supervisor_requests SET status=?, result_json=?, error=?, finished_at=?, "
                             "lease_expires=NULL, requester_woken=1 WHERE id=? AND status='accepted' AND executor=?",
                             (status, json.dumps(results), "requester_gone" if status == "done" else detail, now,
                              req["id"], executor))
            processed.append((int(rid), status))
            continue
        if _finish(conn, req, executor, status, results=results, error=None if status == "done" else detail):
            processed.append((int(rid), status))
        else:
            processed.append((int(rid), "lost_lease"))
    return processed


# --- home side -----------------------------------------------------------------------------------

def _is_home(conn: sqlite3.Connection, own: str, target: str) -> bool:
    from hermes_cli import kanban_capability_routing as kcr
    try:
        row = conn.execute("SELECT created_on FROM tasks WHERE id = ?", (target,)).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and row[0] and row[0] == own and kcr.card_home(conn, target) is None)


def _caller_replica_refusal(rep: sqlite3.Connection, event: dict, payload: dict) -> Optional[str]:
    """The caller checks that need only the replica (no Board read): None when they pass."""
    caller = payload.get("caller") or {}
    gw, task, run_uid = caller.get("gateway"), caller.get("task_id"), caller.get("run_uid")
    at = payload.get("requested_at")
    if not gw or not task or not run_uid or not isinstance(at, (int, float)):
        return "caller_unproven: the request names no caller gateway, card, run uid and time"
    if at > time.time() + SKEW_S:
        return "caller_unproven: the request time is in the future"
    if event["task_id"] != task:
        return "caller_unproven: the request event is not on the caller's card"
    run = rep.execute("SELECT started_at, ended_at FROM task_runs WHERE task_id = ? AND uid = ?",
                      (task, run_uid)).fetchone()
    if run is None or not run[0] or int(run[0]) > at + SKEW_S or (run[1] and int(run[1]) < at - SKEW_S):
        return f"caller_unproven: run {run_uid} of {task} was not live when the call was made"
    claimed = False
    for (raw,) in rep.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'claimed'", (task,)):
        try:
            p = json.loads(raw or "{}")
        except ValueError:
            continue
        if p.get("run_uid") == run_uid and p.get("holder") == gw:
            claimed = True
            break
    if not claimed:
        return f"caller_unproven: the Board never leased run {run_uid} of {task} to {gw}"
    if not event.get("uid"):
        return "caller_unproven: the request event has no Board uid"
    return None


_UNCHECKED = object()


def _caller_refusal(rep: sqlite3.Connection, board: str, event: dict, payload: dict,
                    replica: Any = _UNCHECKED) -> tuple[Optional[str], bool]:
    """``(refusal, retry)``: None when the caller is proven; ``retry`` when the Board could not be read.

    ``replica`` is ``_caller_replica_refusal``'s result when the caller already ran it (``apply_incoming`` runs it
    once per call, before the deferral check), so its task_runs and claimed-event queries are not repeated.
    """
    refusal = _caller_replica_refusal(rep, event, payload) if replica is _UNCHECKED else replica
    if refusal is not None:
        return refusal, False
    gw, task, at = payload["caller"]["gateway"], payload["caller"]["task_id"], payload["requested_at"]
    try:
        ev = _evidence(board, event["uid"])
    except Exception as exc:  # health: allow BLE001 -- unreachable Board: retry next tick
        _log.info("kanban: operator call evidence for %s unavailable (%s); retrying", event["uid"], exc)
        return None, True
    from hermes_cli.kanban_operator_delivery import canonical_payload
    try:
        canonical = canonical_payload(ev, event["uid"], task, REQUEST_KIND, gw)
    except ValueError:
        return "caller_unproven: invalid canonical request identity", False
    if canonical != payload:
        return "caller_unproven: request payload differs from the Board", False
    row = ev["row"]
    created = row.get("created_at")
    when = int(created) if isinstance(created, (int, float)) else int(at)
    if when > 10 ** 11:  # Board timestamps are milliseconds
        when //= 1000
    if time.time() - when > DEADLINE_S:
        return "stale_request: the call is older than 24 h", False
    return None, False


def _marker(conn: sqlite3.Connection, target: str, kind: str, uid: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? AND json_valid(payload) AND "
        "json_extract(payload, '$.operator_request_uid') = ? LIMIT 1", (target, kind, uid)).fetchone() is not None


def _agent_block(conn: sqlite3.Connection, target: str, caller_task: Optional[str] = None) -> Optional[str]:
    """None when the current block is an agent's (a hold David placed by hand is his to release).

    ``caller_task`` is the Board-proven calling card: a missing-parent repair card may release the dispatcher's
    parent_missing block that names it (``kanban_requeue.missing_parent_repair_grant``), and only that block."""
    row = conn.execute("SELECT status, human_request, block_kind FROM tasks WHERE id = ?", (target,)).fetchone()
    if row is None:
        return f"no such task {target}"
    if row[0] != "blocked":
        return f"{target} is {row[0]}, not blocked"
    gate = _human_gate(row[1], row[2], target)
    if gate:
        return gate
    from hermes_cli.kanban_requeue import current_block_payload, missing_parent_repair_grant
    # Positive provenance: only the card's OWN worker run's block counts (kanban_block from that run records actor
    # ``agent (task <target>)``, kanban_cli_actor.tool_actor). A block from a human shell (actor ``user``), the
    # Desktop or dashboard (no actor), a chat agent or the breaker (no blocked event; an ``unblocked`` or
    # ``gave_up`` newer than the last ``blocked`` hides it, ``current_block_payload``) is a hold placed by hand or
    # by policy, and only its owner or a human releases it.
    payload = current_block_payload(conn, target)
    if str(payload.get("actor") or "").strip() != f"agent (task {target})":
        if caller_task and missing_parent_repair_grant(conn, caller_task, target) == caller_task:
            return None
        return (f"{target} was not blocked by its own agent run (a hand, Desktop or policy hold); only its owner "
                "or a human releases it")
    return None


def _apply(conn: sqlite3.Connection, payload: dict, own: str) -> tuple[str, Optional[str]]:
    """Apply one proven call on the home's live file: ``(outcome, detail)``."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_move as km
    uid, target, action = payload["request_uid"], payload["target_task_id"], payload.get("action")
    caller, args = payload.get("caller") or {}, payload.get("args") or {}
    author = f"{caller.get('gateway')}/{caller.get('task_id')}"
    extra = {"operator_request_uid": uid, "operator_caller": {k: caller.get(k) for k in
                                                               ("gateway", "task_id", "run_id", "run_uid")}}
    task = kb.get_task(conn, target)
    if task is None:
        return "refused", f"no such task {target}"
    if action == "creator_archive":
        from hermes_cli.kanban_creator_archive import apply
        return apply(conn, payload, extra)
    if action == "spend_promote":  # its marker answers a replay first; its scope refuses any non-triage card
        from hermes_cli import kanban_spend_promote as ksp
        return ksp.apply(conn, payload, extra)
    if task.status in ("done", "archived"):
        return "refused", f"{target} is {task.status}"
    if action == "comment":
        if _marker(conn, target, COMMENT_MARK, uid):
            return "already_applied", None
        text = str(args.get("text") or "").strip()[:MAX_TEXT]
        if not text:
            return "refused", "empty comment"
        with kb.write_txn(conn):
            cid = kb.add_comment(conn, target, author, text, origin_gateway=caller.get("gateway"), writer_kind="agent")
            kb._append_event(conn, target, COMMENT_MARK, {**extra, "comment_id": cid})
        return "applied", f"comment {cid}"
    if action == "unblock":
        if _marker(conn, target, "unblocked", uid):
            return "already_applied", None
        refusal = _agent_block(conn, target, caller.get("task_id"))
        if refusal:
            return "refused", refusal
        reason = str(args.get("reason") or "").strip()
        from hermes_cli.kanban_requeue import MISSING_PARENT_VIA, missing_parent_repair_grant
        extra = dict(extra)

        def precondition(c):  # runs inside the unblock txn, before unblock_task reads event_extra
            refusal = _agent_block(c, target, caller.get("task_id"))
            if refusal is None and missing_parent_repair_grant(c, caller.get("task_id"), target):
                extra.update(repair_task_id=caller.get("task_id"), via=MISSING_PARENT_VIA)
            return refusal
        ok = kb.unblock_task(conn, target, comment=(author, f"Unblocked through an operator call: {reason}"),
                             event_extra=extra, precondition=precondition)
        if ok and extra.get("via") == MISSING_PARENT_VIA:
            # The same audit row the same-gateway grant writes (``kanban_tools_self_heal._log_requeue``). [daveey]
            with kb.write_txn(conn):
                kb._append_event(conn, target, "missing_parent_requeue", {
                    "by": extra["repair_task_id"], "repair_card": extra["repair_task_id"], "target": target,
                    "operator_request_uid": uid})
        from hermes_cli.kanban_terminal_guard import unblock_refusal
        return ("applied", None) if ok else ("refused", unblock_refusal(conn, target)
                                             or f"cannot unblock {target} (state changed)")
    if action == "move":
        if _marker(conn, target, km.MOVED_EVENT, uid) or _marker(conn, target, MOVE_MARK, uid):
            return "already_applied", None
        out = km.move_queued(conn, target, runs_on=args.get("runs_on") or [], reason=str(args.get("reason") or ""),
                             host_reason=args.get("host_reason"), event_extra=extra)
        if out.get("ok"):
            if not _marker(conn, target, km.MOVED_EVENT, uid):
                # The worker-first Board repin path writes its own event without ``event_extra``: record the
                # operator provenance (and the replay marker) beside it.
                with kb.write_txn(conn):
                    kb._append_event(conn, target, MOVE_MARK, {**extra, "runs_on": out.get("runs_on"),
                                                              "repinned": bool(out.get("repinned"))})
            return "applied", f"runs_on={out.get('runs_on')}"
        return "refused", f"{out.get('reason_code')}: {out.get('error')}"
    return "refused", f"operator_action_refused: {action}"


def _uid_at(rep: sqlite3.Connection, eid: int) -> Optional[str]:
    """The Board uid of replica event ``eid``: names the replica a watermark was taken on."""
    if eid <= 0:
        return None
    row = rep.execute("SELECT uid FROM task_events WHERE id = ?", (eid,)).fetchone()
    return row[0] if row else None


def _anchor(rep: sqlite3.Connection, eid: int) -> tuple[int, Optional[str]]:
    """``(id, uid)`` of the newest event at or below ``eid`` that has a Board uid (0 when none)."""
    row = rep.execute("SELECT id, uid FROM task_events WHERE id <= ? AND uid IS NOT NULL ORDER BY id DESC LIMIT 1",
                      (int(eid),)).fetchone()
    return (int(row[0]), row[1]) if row else (0, None)


def _scan_start(conn: sqlite3.Connection, board: str, rep: sqlite3.Connection) -> int:
    """Resume after the watermark only while the replica still holds the same event at it.

    A retention rebuild recreates the replica and restarts its ids (and a filesystem may reuse the inode, so the
    file is no identity). A watermark whose event uid moved or vanished rescans from 0; the log dedupes.
    """
    row = conn.execute("SELECT last_id, last_uid FROM operator_call_scan WHERE board = ?", (board,)).fetchone()
    candidates = [(int(row[0]), row[1])] if row else []
    mem = _MARK.get((_db_path(conn), board))
    if mem:
        candidates.append(mem)
    for last, uid in sorted(candidates, key=lambda c: c[0], reverse=True):
        if last > 0 and uid and _uid_at(rep, last) == uid:
            return last
    return 0


def _incoming(conn: sqlite3.Connection, own: str, raw: Optional[str]) -> Optional[dict]:
    """The request payload when this gateway executes it and has not yet; None otherwise."""
    from hermes_cli import kanban_operator_labels as kol
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        return None
    uid, target = payload.get("request_uid"), payload.get("target_task_id")
    if payload.get("action") == LABEL_PUBLISH:
        # A machine has no home card: the call names its executor, and only that gateway takes it.
        if not uid or own != kol.HOME or payload.get("home") != own:
            return None
    elif not uid or not target or not _is_home(conn, own, target):
        return None
    if conn.execute("SELECT 1 FROM operator_calls WHERE request_uid = ? AND side = 'execute'", (uid,)).fetchone():
        return None
    return payload


def _home_check(rep: sqlite3.Connection, payload: dict, target,
                replica_refusal: Optional[str]) -> tuple[Optional[str], bool]:
    """``home_refusal`` (unsynced created event: retry, then refuse), only for a caller the replica proves.

    ``replica_refusal`` is ``_caller_replica_refusal``'s result for this call, run once by the caller and passed on
    to ``_caller_refusal``. Replica-only caller checks run first: an unproven caller is refused, never deferred, so
    a forged call (one with a future requested_at too) cannot pin the watermark and page the whole request tail
    every tick.

    Cost of a deferral, bounded: while any hold is set, each tick rescans from the held watermark and pages the
    whole request tail past it (up to MAX_PER_TICK handled calls), and a caller that passes the replica checks but
    would fail the Board evidence check (``_caller_refusal``) is still deferred first, so it can hold the watermark
    for up to ``CREATION_SYNC_GRACE_S`` before it is refused.
    """
    if replica_refusal is not None:
        return None, False  # _caller_refusal refuses it
    from hermes_cli.kanban_consent import home_refusal
    return home_refusal(rep, payload, target)


def apply_incoming(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Home side, each dispatcher tick: execute calls naming this gateway's own cards. Returns (uid, outcome)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_operator_labels as kol
    own = _own_gateway(conn)
    if not own:
        return []
    board = _board_slug(conn)
    done: list[tuple[str, str]] = []
    with _replica(conn) as rep:
        if rep is None:
            return []
        if not _has_log(conn) or not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operator_call_scan'").fetchone() \
                or not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operator_label_jobs'").fetchone():
            with kb.write_txn(conn):
                ensure_schema(conn)
        last = _scan_start(conn, board, rep)
        handled = 0
        hold = None  # below the first call deferred for creation sync lag
        cursor, stop = last, False
        while True:
            rows = rep.execute("SELECT id, uid, task_id, payload FROM task_events WHERE id > ? AND kind = ? "
                               "ORDER BY id LIMIT ?", (cursor, REQUEST_KIND, SCAN_BATCH)).fetchall()
            full = len(rows) >= SCAN_BATCH
            hi = rows[-1][0] if full else rep.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
            for eid, euid, etask, raw in rows:
                payload = _incoming(conn, own, raw)
                if payload is None:
                    continue
                uid, target = payload["request_uid"], payload.get("target_task_id")
                labels = payload.get("action") == LABEL_PUBLISH
                if handled >= MAX_PER_TICK:
                    hi, stop = eid - 1, True
                    break
                event = {"id": eid, "uid": euid, "task_id": etask}
                replica = _caller_replica_refusal(rep, event, payload)
                home = _home_check(rep, payload, target, replica)
                if home[1]:
                    # Only this caller lags: keep the watermark below it (retried next tick) and go on with the
                    # rest. A deferral takes no tick slot and no Board read; calls handled meanwhile are logged,
                    # so the rescan skips them.
                    hold = eid - 1 if hold is None else min(hold, eid - 1)
                    continue
                handled += 1
                refusal, retry = _caller_refusal(rep, board, event, payload, replica)
                if retry:  # unreachable Board: every later call would retry too
                    hi, stop = eid - 1, True
                    break
                if refusal is None:
                    refusal = home[0]
                if labels:
                    done.append((uid, kol.handle(conn, own, event, payload, refusal)))
                    continue
                if payload.get("action") == "protect_stopped":
                    from hermes_cli import kanban_protect_operator as protection
                    outcome = protection.handle(conn, own, event, payload, refusal)
                else:
                    outcome, _detail = _execute_one(conn, payload, own, refusal)
                done.append((uid, outcome))
            # A held watermark rescans from the deferred call each tick: page on past it, or calls more than
            # SCAN_BATCH request rows later would wait out the deferral's whole grace.
            if stop or not full or hold is None:
                break
            cursor = rows[-1][0]
        if hold is not None:
            hi = min(int(hi), hold)
        stored = conn.execute("SELECT last_id, last_uid FROM operator_call_scan WHERE board = ?",
                              (board,)).fetchone()
        new, anchor = _anchor(rep, max(int(hi), last))
        key = (_db_path(conn), board)
        _MARK[key] = (new, anchor)
        # The watermark lives in memory between ticks; it is persisted when a call was handled or every 10 min,
        # so an idle board costs no write per tick (a restart rescans at most that window; the log dedupes).
        if stored is None or ((int(stored[0]), stored[1]) != (new, anchor)
                              and (done or time.time() - _PERSISTED.get(key, 0) > 600)):
            with kb.write_txn(conn):
                conn.execute("INSERT INTO operator_call_scan (board, last_id, last_uid) VALUES (?, ?, ?) "
                             "ON CONFLICT(board) DO UPDATE SET last_id = excluded.last_id, "
                             "last_uid = excluded.last_uid", (board, new, anchor))
            _PERSISTED[key] = time.time()
    if own == kol.HOME:
        kol.tick(conn, own, board)  # start queued label jobs, answer finished ones on the requesters' cards
    from hermes_cli import kanban_protect_operator as protection
    done.extend(protection.tick(conn, own, board))
    return done


def _execute_one(conn: sqlite3.Connection, payload: dict, own: str, refusal: Optional[str]) -> tuple[str, str]:
    from hermes_cli import kanban_db as kb
    uid, target = payload["request_uid"], payload["target_task_id"]
    action = payload.get("action")
    op = {"action": action, "task_id": target, **(payload.get("args") or {})}
    if refusal is None and action not in ACTIONS:
        refusal = f"operator_action_refused: {action} stays with the card's owner"
    proof = None
    if refusal is None:
        try:
            outcome, detail = _apply(conn, payload, own)
            if action == "comment" and outcome in ("applied", "already_applied"):
                from hermes_cli.kanban_operator_delivery import applied_proof
                proof = applied_proof(conn, payload, own)
        except Exception as exc:  # health: allow BLE001 -- recorded as a failed call, never silent
            _log.exception("kanban: operator call %s on %s failed", uid, target)
            outcome, detail = "failed", f"{type(exc).__name__}: {exc}"[:500]
    else:
        outcome, detail = "refused", refusal
    with kb.write_txn(conn):
        log(conn, request_uid=uid, side="execute", outcome=outcome, op=op, caller=payload.get("caller"),
            detail=detail, board=payload.get("board"), executor_gateway=own,
            supervisor_request_id=payload.get("supervisor_request_id"), finished=True)
        kb._append_event(conn, target, RESULT_KIND, {
            "request_uid": uid, "action": action, "outcome": outcome, "detail": detail, "executor_gateway": own,
            **({"operator_delivery": proof} if proof else {}),
            "caller": {k: (payload.get("caller") or {}).get(k) for k in ("gateway", "task_id", "run_id", "run_uid")}})
    _log.info("kanban: operator call %s %s on %s from %s: %s", uid, action, target,
              (payload.get("caller") or {}).get("gateway"), outcome)
    return outcome, detail or ""


def list_calls(conn: sqlite3.Connection, *, task: Optional[str] = None, uid: Optional[str] = None,
               limit: int = 50) -> list[dict]:
    if not _has_log(conn):
        return []
    sql, params = "SELECT * FROM operator_calls WHERE 1=1", []
    if task:
        sql += " AND (caller_task_id = ? OR target_task_id = ?)"
        params += [task, task]
    if uid:
        sql += " AND request_uid = ?"
        params.append(uid)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
