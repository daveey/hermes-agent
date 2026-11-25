"""Durable supervisor requests: a scoped worker asks a non-worker executor for graph edits. [daveey]

A dispatcher worker may only remove its own card's edges (``kanban_unlink``), and the
assignment-repair route (``kanban_assignment_routing``) only ever hands work to another
*task-scoped worker*, which has the same wall. So a worker that created a graph of
cards and must re-order it had no supported route: ``no_capable_supervisor`` when the
creator and assignee profile are the same, and a useless second worker otherwise.

This module is the narrow contract between the two:

* **Request** (``submit``): a typed list of ``unlink`` ops (``kanban_link`` is open to
  workers already, so link ops are only previewed, verdict ``self``), from a live
  dispatcher worker run (origin), touching only the requester's own card or cards the
  requester's task created (scope). Batches are simulated for cycles up front and
  deduplicated per requester. A request is a row in ``supervisor_requests``; it is
  never a human ask and never a chat turn.
* **Executor** (``drain``): runs only in a non-worker process (the gateway dispatcher
  tick, or ``hermes kanban supervisor-requests --drain``). It accepts a request with a
  lease (compare-and-swap, so a busy executor's request stays queued for it), re-checks
  scope against the board as it is now, applies each op through the ordinary
  ``kanban_link`` / ``kanban_unlink`` tool handlers, and records a correlated result.
  A crash after acceptance leaves the row ``accepted``; the lease expiring (or the
  executor pid being gone on this host) puts it back up for redelivery, and ops are
  idempotent (an edge already present/absent counts as applied). A request no executor accepted
  within ``DEADLINE_S`` (24 h) is rejected at accept as ``expired``, never run late.
* **reconcile_notify** ops (``kanban_notify_reconcile_exec``) retire a wrong-local refused-send tui
  subscription. Their execution waits on the owner gateway's own receipt read (phase
  ``awaiting_owner``), which ``drain`` polls without spending delivery attempts.
* **resume** ops (``kanban_resume_exec``) requeue the card that spawned the requester when the
  continuation budget parked it (``continuation_exhausted``), through ``kanban_db.unblock_task``.
  Never a human-gated card; scope is the requester's creator chain; capped per target.
* **retry_limit** ops (``kanban_retry_limit_exec``) set ``max_retries`` (1-10) on a card the requester
  created, never its own, through ``kanban_db_operator.edit_operator_fields``.
* **archive** ops (``kanban_archive_exec``) cancel an unstarted card orphaned in the requester's creator
  tree (created by a finished card the requester created), through ``kanban_db.archive_task``.
* **Receipt**: every transition appends a ``supervisor_request_*`` event, and the
  terminal one a comment, on the requester's card. A requester parked ``scheduled`` is
  woken, also when it parks only after the result landed. A result never completes, unblocks or releases the requester; a rejected or
  failed request only reports.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import sqlite3
import time
from typing import Any, Callable, Optional, cast

_log = logging.getLogger(__name__)

MAX_OPS = 20
MAX_ATTEMPTS = 5
LEASE_SECONDS = 120
EXECUTOR_FRESH_SECONDS = 300
# A request no executor accepted within this long is rejected as expired at accept, not run late (the same
# 24 h cap kanban_operator_call.DEADLINE_S puts on a forwarded call at its home gateway).
DEADLINE_S = 24 * 3600
OPS = ("link", "unlink", "reconcile_notify", "resume", "retry_limit", "archive")  # link: preview only (dry_run); a request carries unlink ops
# reconcile_notify: retire a wrong-local refused-send tui subscription on owner-read receipts
# (hermes_cli/kanban_notify_reconcile_exec.py). A request carries only those, or only edge ops.
# resume: requeue a continuation_exhausted card in the requester's creator chain (kanban_resume_exec.py).
# retry_limit: set max_retries on a card the requester created (kanban_retry_limit_exec.py).
# archive: cancel an unstarted orphaned descendant in the requester's creator tree (kanban_archive_exec.py).
ACTIVE = ("pending", "accepted")
TERMINAL = ("done", "rejected", "failed")
COMMENT_AUTHOR = "kanban-supervisor"

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS supervisor_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        requester_task_id TEXT NOT NULL,
        requester_run_id INTEGER,
        ops_json TEXT NOT NULL,
        reason TEXT,
        dedupe_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        executor TEXT,
        lease_expires INTEGER,
        attempts INTEGER NOT NULL DEFAULT 0,
        result_json TEXT,
        error TEXT,
        created_at INTEGER NOT NULL,
        accepted_at INTEGER,
        finished_at INTEGER,
        requester_woken INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS idx_supervisor_requests_status ON supervisor_requests(status, id)",
    "CREATE INDEX IF NOT EXISTS idx_supervisor_requests_requester ON supervisor_requests(requester_task_id)",
    "CREATE INDEX IF NOT EXISTS idx_supervisor_requests_unwoken ON supervisor_requests(id) WHERE requester_woken=0",
    """CREATE TABLE IF NOT EXISTS supervisor_executors (
        executor_id TEXT PRIMARY KEY,
        role TEXT,
        last_seen INTEGER NOT NULL)""",
)


class SupervisorRequestError(ValueError):
    """A request that cannot be accepted. ``code`` is a stable machine-readable class."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


# --- schema ------------------------------------------------------------------------------------

def ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in _SCHEMA:
        conn.execute(stmt)


def _has_table(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='supervisor_requests'").fetchone() is not None


# --- validation --------------------------------------------------------------------------------

def normalize_ops(ops: Any) -> list[dict]:
    if not isinstance(ops, list) or not ops:
        raise SupervisorRequestError("invalid_ops", "ops must be a non-empty list of {op, parent_id, child_id}")
    if len(ops) > MAX_OPS:
        raise SupervisorRequestError("invalid_ops", f"at most {MAX_OPS} ops per request")
    out = []
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise SupervisorRequestError("invalid_ops", f"op {i} is not an object")
        kind = str(op.get("op") or "").strip()
        if kind == "reconcile_notify":
            from hermes_cli import kanban_notify_reconcile_exec as nrx
            out.append(nrx.normalize_op(i, op))
            continue
        if kind == "resume":
            from hermes_cli import kanban_resume_exec as rx
            out.append(rx.normalize_op(i, op))
            continue
        if kind == "retry_limit":
            from hermes_cli import kanban_retry_limit_exec as rlx
            out.append(rlx.normalize_op(i, op))
            continue
        if kind == "archive":
            from hermes_cli import kanban_archive_exec as ax
            out.append(ax.normalize_op(i, op))
            continue
        if kind == "operator":
            from hermes_cli import kanban_operator_call as kox
            if len(ops) != 1:
                raise SupervisorRequestError("invalid_ops", "an operator call names exactly one card: one op per "
                                                            "request")
            out.append(kox.normalize_op(i, op))
            continue
        parent = str(op.get("parent_id") or "").strip()
        child = str(op.get("child_id") or "").strip()
        if kind not in OPS:
            raise SupervisorRequestError("invalid_ops", f"op {i}: op must be one of {', '.join(OPS)}")
        if not parent or not child or parent == child:
            raise SupervisorRequestError("invalid_ops", f"op {i}: distinct parent_id and child_id are required")
        out.append({"op": kind, "parent_id": parent, "child_id": child})
    if len({_family(o) for o in out}) > 1:
        raise SupervisorRequestError(
            "invalid_ops", "a request carries only reconcile_notify ops, only resume ops, only retry_limit ops, "
            "only archive ops, or only edge ops")
    return out


_OWN_FAMILIES = ("reconcile_notify", "resume", "retry_limit", "archive", "operator")


def _family(op: dict) -> str:
    return op["op"] if op.get("op") in _OWN_FAMILIES else "edge"


def _is_resume(ops: list[dict]) -> bool:
    return bool(ops) and ops[0].get("op") == "resume"


def _is_retry_limit(ops: list[dict]) -> bool:
    return bool(ops) and ops[0].get("op") == "retry_limit"


def _is_archive(ops: list[dict]) -> bool:
    return bool(ops) and ops[0].get("op") == "archive"


def _is_reconcile(ops: list[dict]) -> bool:
    return bool(ops) and ops[0].get("op") == "reconcile_notify"


def _is_operator(ops: list[dict]) -> bool:
    return bool(ops) and ops[0].get("op") == "operator"


def op_label(op: dict) -> str:
    if op.get("op") == "operator":
        from hermes_cli import kanban_operator_call as kox
        return kox.label(op)
    if op.get("op") == "resume":
        from hermes_cli import kanban_resume_exec as rx
        return rx.label(op)
    if op.get("op") == "retry_limit":
        from hermes_cli import kanban_retry_limit_exec as rlx
        return rlx.label(op)
    if op.get("op") == "archive":
        from hermes_cli import kanban_archive_exec as ax
        return ax.label(op)
    from hermes_cli import kanban_notify_reconcile_exec as nrx
    return nrx.label(op)


def dedupe_key(requester: str, ops: list[dict]) -> str:
    return hashlib.sha256(json.dumps([requester, ops], sort_keys=True).encode()).hexdigest()


def _created_by_task(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' ORDER BY id ASC LIMIT 1",
        (task_id,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0]).get("creator_task_id")
    except (TypeError, ValueError):
        return None


def endpoint_scope(conn: sqlite3.Connection, requester: str, task_id: str) -> Optional[str]:
    """None when ``task_id`` is inside the requester's scope, else the refusal."""
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return f"unknown task {task_id}"
    if row[0] == "archived":
        return f"{task_id} is archived"
    if task_id == requester:
        return None
    if _created_by_task(conn, task_id) != requester:
        return (f"{task_id} was not created by {requester}; a supervisor request may only touch the "
                "requester's own card and cards its task created")
    return None


def requester_refusal(conn: sqlite3.Connection, requester: str) -> Optional[str]:
    """Only a missing or archived requester voids its queued requests. A ``done`` requester's request still
    runs: ``submit`` proved a live run, filing a comment is naturally a worker's last act, and each executor
    re-checks the op's scope against the board as it is now."""
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (requester,)).fetchone()
    if row is None:
        return f"requester {requester} no longer exists"
    if row[0] == "archived":
        return f"requester {requester} is {row[0]}"
    return None


def _edges(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    return {(r[0], r[1]) for r in conn.execute("SELECT parent_id, child_id FROM task_links")}


def _reaches(edges: set[tuple[str, str]], start: str, goal: str) -> bool:
    children: dict[str, list[str]] = {}
    for p, c in edges:
        children.setdefault(p, []).append(c)
    seen, stack = set(), [start]
    while stack:
        node = stack.pop()
        if node == goal:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack.extend(children.get(node, ()))
    return False


def preflight(conn: sqlite3.Connection, requester: str, ops: list[dict]) -> list[dict]:
    """Per-op verdict: ``self`` (the worker may do it with kanban_link/unlink itself),
    ``supervisor`` (in scope, needs the executor), ``noop`` (already in that state) or
    ``refused`` with a reason. Ops are simulated in order on the current graph."""
    if _is_reconcile(ops):
        # No creator-scope rule: the owner-read receipt gate is the safety (it only retires a row the
        # owner gateway proves delivered), and any worker may hit a phantom unseen row on any card.
        from hermes_cli import kanban_notify_reconcile_exec as nrx
        return [nrx.preflight_op(conn, op) for op in ops]
    if _is_resume(ops):
        from hermes_cli import kanban_resume_exec as rx
        return [rx.preflight_op(conn, requester, op) for op in ops]
    if _is_retry_limit(ops):
        from hermes_cli import kanban_retry_limit_exec as rlx
        return [rlx.preflight_op(conn, requester, op) for op in ops]
    if _is_archive(ops):
        from hermes_cli import kanban_archive_exec as ax
        return [ax.preflight_op(conn, requester, op) for op in ops]
    if _is_operator(ops):
        from hermes_cli import kanban_operator_call as kox
        return [kox.preflight_op(conn, requester, op) for op in ops]
    edges = _edges(conn)
    verdicts = []
    for op in ops:
        p, c = op["parent_id"], op["child_id"]
        refusal = endpoint_scope(conn, requester, p) or endpoint_scope(conn, requester, c)
        verdict: dict = {**op}
        if op["op"] == "link":
            # kanban_link has no worker ownership rule: the worker links directly. Simulated so later
            # unlink verdicts see the graph as it will be.
            if (p, c) in edges:
                verdict.update(verdict="noop", reason="edge already present")
            elif _reaches(edges, c, p):
                verdict.update(verdict="refused", reason=f"linking {p} -> {c} would create a cycle")
            else:
                edges.add((p, c))
                verdict.update(verdict="self", reason="use kanban_link")
        elif refusal:
            verdict.update(verdict="refused", reason=refusal)
        else:
            if (p, c) not in edges:
                verdict.update(verdict="noop", reason="edge already absent")
            else:
                edges.discard((p, c))
                verdict["verdict"] = "self" if requester in (p, c) else "supervisor"
        verdicts.append(verdict)
    return verdicts


def validate_origin(conn: sqlite3.Connection, env_task: Optional[str], env_run: Optional[str]) -> tuple[str, int]:
    """The requester task and run, proven against the board. Only a live dispatcher worker run may ask."""
    if not env_task:
        raise SupervisorRequestError(
            "not_a_worker", "only a dispatcher worker run files supervisor requests; an orchestrator or "
            "operator has board-wide kanban_link/kanban_unlink already")
    try:
        run_id = int(env_run or "")
    except ValueError:
        raise SupervisorRequestError("unbound_worker", "HERMES_KANBAN_RUN_ID is not set to this worker's run") from None
    row = conn.execute("SELECT status, current_run_id FROM tasks WHERE id = ?", (env_task,)).fetchone()
    if row is None:
        raise SupervisorRequestError("unknown_requester", f"no such task {env_task}")
    if row[0] != "running" or row[1] != run_id:
        raise SupervisorRequestError(
            "stale_run", f"run {run_id} no longer owns {env_task} (status={row[0]}, current run={row[1]})")
    return env_task, run_id


# --- request side ------------------------------------------------------------------------------

def executor_available(conn: sqlite3.Connection, *, now: Optional[int] = None) -> bool:
    now = int(time.time()) if now is None else now
    try:
        row = conn.execute("SELECT MAX(last_seen) FROM supervisor_executors").fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(row and row[0] and now - int(row[0]) <= EXECUTOR_FRESH_SECONDS)


def _row(r) -> dict:
    d = dict(r)
    for key in ("ops_json", "result_json"):
        raw = d.pop(key, None)
        d[key[:-5]] = json.loads(raw) if raw else None
    return d


def get_request(conn: sqlite3.Connection, request_id: int) -> Optional[dict]:
    if not _has_table(conn):
        return None
    r = conn.execute("SELECT * FROM supervisor_requests WHERE id = ?", (int(request_id),)).fetchone()
    return _row(r) if r else None


def list_requests(conn: sqlite3.Connection, *, requester: Optional[str] = None,
                  status: Optional[str] = None, limit: int = 50) -> list[dict]:
    if not _has_table(conn):
        return []
    sql, params = "SELECT * FROM supervisor_requests WHERE 1=1", []
    if requester:
        sql += " AND requester_task_id = ?"
        params.append(requester)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    return [_row(r) for r in conn.execute(sql, params)]


def submit(conn: sqlite3.Connection, *, env_task: Optional[str], env_run: Optional[str], ops: Any,
           reason: Optional[str] = None, dry_run: bool = False) -> dict:
    """File (or, with ``dry_run``, only preflight) a request. Duplicates return the existing row.
    A refused operator call (``kanban_operator_call``) is logged before the refusal is raised."""
    try:
        return _submit(conn, env_task=env_task, env_run=env_run, ops=ops, reason=reason, dry_run=dry_run)
    except SupervisorRequestError as exc:
        from hermes_cli import kanban_operator_call as kox
        if not dry_run and kox.is_operator(ops):
            kox.log_submit_refusal(conn, env_task=env_task, env_run=env_run, raw_ops=ops, code=exc.code,
                                   message=str(exc))
        raise


def _submit(conn: sqlite3.Connection, *, env_task: Optional[str], env_run: Optional[str], ops: Any,
            reason: Optional[str] = None, dry_run: bool = False) -> dict:
    from hermes_cli import kanban_db as kb

    norm = normalize_ops(ops)
    requester, run_id = validate_origin(conn, env_task, env_run)
    from hermes_cli.kanban_consent import current_session, enforce
    enforce(conn, requester, norm, session=current_session())  # creation stops + this session's own stops
    verdicts = preflight(conn, requester, norm)
    available = executor_available(conn)
    summary = {"ops": verdicts, "executor_available": available}
    refused = [v for v in verdicts if v["verdict"] == "refused"]
    if dry_run:
        return {"dry_run": True, **summary}
    key = dedupe_key(requester, norm)
    # Only a request still in flight is a duplicate: a finished one is history, and filing the same
    # ops again (an edge re-added since) is a new request.
    dup_sql = ("SELECT id, status FROM supervisor_requests WHERE dedupe_key = ? AND status IN "
               "('pending','accepted') ORDER BY id DESC LIMIT 1")
    existing = conn.execute(dup_sql, (key,)).fetchone() if _has_table(conn) else None
    if existing:
        return {"request_id": int(existing[0]), "status": existing[1], "duplicate": True, **summary}
    links = [v for v in verdicts if v["op"] == "link"]
    if links:
        raise SupervisorRequestError(
            "link_not_requested", "; ".join(f"link {v['parent_id']} -> {v['child_id']}" for v in links)
            + ": link ops are preview-only (dry_run); add edges yourself with kanban_link and request "
            "only unlinks")
    if refused:
        raise SupervisorRequestError(
            "refused", "; ".join(f"{op_label(v)}: {v['reason']}" for v in refused))
    own = [v for v in verdicts if v["verdict"] == "self"]
    if own:
        raise SupervisorRequestError(
            "own_card_op", "; ".join(f"{v['op']} {v['parent_id']} -> {v['child_id']}" for v in own)
            + ": no supervisor needed; unlink this worker's own card's edges with kanban_unlink and "
            "request only the rest")
    if not any(v["verdict"] == "supervisor" for v in verdicts):
        raise SupervisorRequestError("no_supervisor_needed", "every op is already in the requested state")
    with kb.write_txn(conn):
        ensure_schema(conn)
        existing = conn.execute(dup_sql, (key,)).fetchone()
        if existing:
            return {"request_id": int(existing[0]), "status": existing[1], "duplicate": True, **summary}
        now = int(time.time())
        cur = conn.execute(
            "INSERT INTO supervisor_requests (requester_task_id, requester_run_id, ops_json, reason, "
            "dedupe_key, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (requester, run_id, json.dumps(norm), (reason or "").strip()[:2000] or None, key, now))
        rid = int(cur.lastrowid or 0)
        kb._append_event(conn, requester, "supervisor_request_queued", {
            "request_id": rid, "ops": norm, "executor_available": available})
    return {"request_id": rid, "status": "pending", "duplicate": False, **summary}


# --- executor side -----------------------------------------------------------------------------

def _non_worker_refusal() -> Optional[str]:
    if os.environ.get("HERMES_KANBAN_TASK"):
        return "a dispatcher worker cannot execute supervisor requests (HERMES_KANBAN_TASK is set)"
    try:
        from agent import delegation_context
        delegated = delegation_context.is_delegated_child_process_context()
    except Exception:  # health: allow BLE001 -- unknown context: fail closed
        delegated = True
    if delegated:
        return "a delegate_task child cannot execute supervisor requests"
    return None


def executor_identity(role: str) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{role}"


def _executor_dead(executor: Optional[str]) -> bool:
    """True only when the holder ran on this host and its pid is gone (a crash, not a busy peer)."""
    if not executor:
        return True
    parts = executor.split(":")
    if len(parts) < 2 or parts[0] != socket.gethostname():
        return False
    try:
        pid = int(parts[1])
    except ValueError:
        return False
    if pid == os.getpid():
        return False
    import psutil
    return not psutil.pid_exists(pid)


def _after_accept(request: dict) -> None:
    """Seam between acceptance and execution (crash-injection point for tests)."""


def _apply_op(op: dict, board: Optional[str]) -> dict:
    """Apply one op through the ordinary kanban tool handler, idempotently."""
    from tools import kanban_tools as kt

    handler = kt._handle_link if op["op"] == "link" else kt._handle_unlink
    args = {"parent_id": op["parent_id"], "child_id": op["child_id"]}
    if board:
        args["board"] = board
    out = json.loads(handler(args))
    return out


def _accept_next(conn: sqlite3.Connection, executor: str, now: int,
                 abandoned: list, expired: list) -> Optional[dict]:
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        for r in conn.execute(
                "SELECT id, status, executor, lease_expires, result_json, created_at, attempts "
                "FROM supervisor_requests WHERE status IN ('pending','accepted') ORDER BY id ASC").fetchall():
            rid, status, holder, lease = int(r[0]), r[1], r[2], r[3]
            # Never accepted (attempts 0: pending, or an _expire that crashed before its receipt) in time:
            # rejected by the caller (_expire), never run late.
            if int(r[6]) == 0 and now - int(r[5]) > DEADLINE_S:
                expired.append((rid, holder))
                continue
            if status == "accepted" and _awaiting_owner(r[4]):
                continue  # waiting on another gateway: polled (kanban_notify_reconcile_exec.poll), no attempt spent
            if status == "accepted" and lease and int(lease) > now and not _executor_dead(holder):
                continue  # a live executor holds it: stays queued for that executor
            attempts = int(conn.execute("SELECT attempts FROM supervisor_requests WHERE id=?",
                                        (rid,)).fetchone()[0])
            if attempts >= MAX_ATTEMPTS:
                abandoned.append((rid, holder))
                continue
            cur = conn.execute(
                "UPDATE supervisor_requests SET status='accepted', executor=?, lease_expires=?, "
                "attempts=attempts+1, accepted_at=? WHERE id=? AND status=? AND COALESCE(executor,'')=?",
                (executor, now + LEASE_SECONDS, now, rid, status, holder or ""))
            if cur.rowcount != 1:
                continue
            req = _row(conn.execute("SELECT * FROM supervisor_requests WHERE id=?", (rid,)).fetchone())
            kb._append_event(conn, req["requester_task_id"], "supervisor_request_accepted", {
                "request_id": rid, "executor": executor, "attempt": req["attempts"],
                **({"redelivered_from": holder} if holder else {})})
            return req
    return None


def _awaiting_owner(raw: Optional[str]) -> bool:
    """Waiting on another gateway: reconcile_notify (``awaiting_owner``) or an operator call (``awaiting_home``)."""
    try:
        return isinstance(raw, str) and json.loads(raw).get("phase") in ("awaiting_owner", "awaiting_home")
    except (ValueError, AttributeError):
        return False


def _finish(conn: sqlite3.Connection, req: dict, executor: str, status: str, *,
            results: list[dict], error: Optional[str]) -> bool:
    from hermes_cli import kanban_db as kb

    requester = req["requester_task_id"]
    rid = req["id"]
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE supervisor_requests SET status=?, result_json=?, error=?, finished_at=?, lease_expires=NULL "
            "WHERE id=? AND status='accepted' AND executor=?",
            (status, json.dumps(results), error, int(time.time()), rid, executor))
        if cur.rowcount != 1:
            return False  # lost the lease to another executor; that one reports
        payload = {"request_id": rid, "executor": executor, "results": results}
        if error:
            payload["error"] = error
        kb._append_event(conn, requester, f"supervisor_request_{status}", payload)
        lines = [f"Supervisor request #{rid} {status} (executor {executor})."]
        lines += [f"- {op_label(r)}: {r['outcome']}"
                  + (f" ({r['detail']})" if r.get("detail") else "") for r in results]
        if error:
            lines.append(f"Error: {error}")
        if status != "done":
            lines.append("Nothing about this card's own acceptance changed; decide the next step.")
        kb.add_comment(conn, requester, COMMENT_AUTHOR, "\n".join(lines))
        _wake_requester(conn, rid, requester, status)
    return True


def _park_names_request(conn: sqlite3.Connection, requester: str, rid: int, since: int) -> bool:
    """Whether the requester's current park came after the result and its reason names ``#<rid>``."""
    import re
    from hermes_cli.kanban_continuation import SCHEDULED_ENTRY_EVENTS
    marks = ",".join("?" for _ in SCHEDULED_ENTRY_EVENTS)
    row = conn.execute(
        f"SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind IN ({marks}) "
        "ORDER BY id DESC LIMIT 1", (requester, *SCHEDULED_ENTRY_EVENTS)).fetchone()
    return bool(row and int(row[1] or 0) >= since and re.search(rf"#{rid}\b", str(row[0] or "")))


def _wake_requester(conn: sqlite3.Connection, rid: int, requester: str, status: str, *,
                    late_since: Optional[int] = None) -> None:
    """Wake a parked requester once per result. A requester still running when the result lands
    is woken later (:func:`_rewake_late_parkers`) only by a park that names this request."""
    from hermes_cli import kanban_wake_check as kwc
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (requester,)).fetchone()
    state = row[0] if row else None
    if state == "running":
        return  # not parked yet: leave requester_woken=0 for the next drain
    if state == "scheduled" and (late_since is None or _park_names_request(conn, requester, rid, late_since)):
        kwc._wake(conn, requester, "supervisor_request_wake", {"request_id": rid, "request_status": status})
    conn.execute("UPDATE supervisor_requests SET requester_woken=1 WHERE id=?", (rid,))


def _rewake_late_parkers(conn: sqlite3.Connection) -> None:
    from hermes_cli import kanban_db as kb
    sql = ("SELECT id, requester_task_id, status, finished_at FROM supervisor_requests WHERE requester_woken=0 "
           "AND status IN ('done','rejected','failed') ORDER BY id")
    if not conn.execute(sql).fetchone():
        return
    with kb.write_txn(conn):
        for rid, requester, status, finished in conn.execute(sql).fetchall():
            _wake_requester(conn, int(rid), requester, status, late_since=int(finished or 0))


def _execute(conn: sqlite3.Connection, req: dict, executor: str, board: Optional[str]) -> str:
    requester = req["requester_task_id"]
    ops = req["ops"]
    if _is_reconcile(ops):
        from hermes_cli import kanban_notify_reconcile_exec as nrx
        return nrx.start(conn, req, executor)
    if _is_resume(ops):
        from hermes_cli import kanban_resume_exec as rx
        return rx.execute(conn, req, executor)
    if _is_retry_limit(ops):
        from hermes_cli import kanban_retry_limit_exec as rlx
        return rlx.execute(conn, req, executor)
    if _is_archive(ops):
        from hermes_cli import kanban_archive_exec as ax
        return ax.execute(conn, req, executor)
    if _is_operator(ops):
        from hermes_cli import kanban_operator_call as kox
        return kox.start(conn, req, executor)
    # Scope is re-checked against the board as it is now, not as it was at submit time.
    gone = requester_refusal(conn, requester)
    verdicts = preflight(conn, requester, ops)
    refused = [v for v in verdicts if v["verdict"] == "refused"]
    if gone or refused or any(op["op"] != "unlink" for op in ops):
        results = [{**{k: v[k] for k in ("op", "parent_id", "child_id")}, "outcome": "not_applied",
                    "detail": v.get("reason")} for v in verdicts]
        reasons = ([gone] if gone else []) + [v["reason"] for v in refused]
        _finish(conn, req, executor, "rejected", results=results,
                error="; ".join(reasons) or "only unlink ops are executed")
        return "rejected"
    results, error = [], None
    for op in ops:
        edges = _edges(conn)
        present = (op["parent_id"], op["child_id"]) in edges
        if (op["op"] == "link") == present:
            results.append({**op, "outcome": "already_applied"})
            continue
        if error:
            results.append({**op, "outcome": "not_applied", "detail": "an earlier op failed"})
            continue
        out = _apply_op(op, board)
        if out.get("ok"):
            results.append({**op, "outcome": "applied", **({"gated": True} if out.get("gated") else {})})
        else:
            error = str(out.get("error") or out)[:500]
            results.append({**op, "outcome": "failed", "detail": error})
    status = "failed" if error else "done"
    _finish(conn, req, executor, status, results=results, error=error)
    return status


def drain(conn: sqlite3.Connection, *, board: Optional[str] = None, role: str = "dispatcher",
          limit: int = 10, now: Optional[int] = None,
          on_accept: Optional[Callable[[dict], None]] = None, incoming: bool = True) -> dict:
    """Execute queued requests from a non-worker process. Returns ``{executor, processed: [(id, status)]}``.

    Refuses (without touching anything) inside a worker or delegated child."""
    from hermes_cli import kanban_db as kb

    refusal = _non_worker_refusal()
    if refusal:
        raise SupervisorRequestError("worker_context", refusal)
    executor = executor_identity(role)
    now = int(time.time()) if now is None else now
    with kb.write_txn(conn):
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO supervisor_executors (executor_id, role, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT(executor_id) DO UPDATE SET last_seen=excluded.last_seen, role=excluded.role "
            "WHERE supervisor_executors.last_seen < excluded.last_seen - 60",
            (executor, role, now))
    processed = []
    _rewake_late_parkers(conn)
    for _ in range(max(0, int(limit))):
        abandoned: list = []
        expired: list = []
        req = _accept_next(conn, executor, now, abandoned, expired)
        for rid, holder in expired:
            if _expire(conn, rid, holder, executor):
                processed.append((rid, "rejected"))
        for rid, holder in abandoned:
            if _abandon(conn, rid, holder, executor):
                processed.append((rid, "failed"))
        if req is None:
            break
        (on_accept or _after_accept)(req)
        try:
            status = _execute(conn, req, executor, board)
        except Exception as exc:  # health: allow BLE001 -- recorded as a failed request, never silent
            _log.exception("supervisor request %s failed", req["id"])
            _finish(conn, req, executor, "failed", results=[], error=f"{type(exc).__name__}: {exc}"[:500])
            status = "failed"
        processed.append((req["id"], status))
    from hermes_cli import kanban_notify_reconcile_exec as nrx
    processed += nrx.poll(conn, executor, now=now)
    from hermes_cli import kanban_operator_call as kox
    processed += kox.poll(conn, executor, now=now)
    try:  # home side: calls other gateways' workers made on this gateway's cards
        if incoming:
            kox.apply_incoming(conn)
    except Exception:  # health: allow BLE001 -- the requests stay in the replica and are retried next drain
        _log.warning("kanban: applying incoming operator calls failed", exc_info=True)
    return {"executor": executor, "processed": processed}


def _abandon(conn: sqlite3.Connection, rid: int, holder: Optional[str], executor: str) -> bool:
    """Fail a request whose executors kept dying on it (``MAX_ATTEMPTS`` acceptances). Takes it only
    from the holder it was found expired with (compare-and-swap), so a holder that renewed keeps it."""
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE supervisor_requests SET status='accepted', executor=? WHERE id=? AND "
            "status IN ('pending','accepted') AND COALESCE(executor,'')=?", (executor, rid, holder or ""))
        if cur.rowcount != 1:
            return False
    return _finish(conn, get_request(conn, rid), executor, "failed", results=[],
                   error=f"abandoned after {MAX_ATTEMPTS} deliveries whose executor never finished")


def _expire(conn: sqlite3.Connection, rid: int, holder: Optional[str], executor: str) -> bool:
    """Reject a request no executor accepted within ``DEADLINE_S`` without running any op. Takes it only from
    the holder it was found with (compare-and-swap; attempts stays 0, so a crash here is expired again next
    drain). The requester gets the usual rejection receipt."""
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE supervisor_requests SET status='accepted', executor=? WHERE id=? AND attempts=0 AND "
            "status IN ('pending','accepted') AND COALESCE(executor,'')=?", (executor, rid, holder or ""))
        if cur.rowcount != 1:
            return False
    req = cast(dict, get_request(conn, rid))  # the row the CAS above just took
    detail = f"expired: pending > DEADLINE_S ({DEADLINE_S // 3600} h) with no executor accepting it"
    results = [{**op, "outcome": "not_applied", "detail": "expired"} for op in req["ops"] or []]
    return _finish(conn, req, executor, "rejected", results=results, error=detail)


_skip_logged = False


def dispatcher_drain(conn: sqlite3.Connection, *, board: Optional[str] = None) -> list:
    """Dispatcher-tick entry: propagates pass cancellation. Config ``kanban.supervisor_requests.enabled: false``
    (default on, not in config_defaults) leaves requests pending for ``supervisor-requests --drain``."""
    from hermes_cli.kanban_dispatch_storage import DispatchWriteContended
    try:
        from hermes_cli import kanban_supervisor_gate
        refusal = _non_worker_refusal()
        if refusal:
            global _skip_logged
            if not _skip_logged:  # a gateway that inherited worker env would never drain: say so once
                _skip_logged = True
                _log.warning("kanban: not draining supervisor requests in this process: %s", refusal)
            return []
        return kanban_supervisor_gate.dispatch(conn, board)
    except DispatchWriteContended:
        raise
    except Exception:  # health: allow BLE001 -- the tick must go on; the rows stay queued
        _log.warning("kanban: draining supervisor requests failed", exc_info=True)
        return []


def cli_command(args) -> int:
    """``hermes kanban supervisor-requests``: list requests, or with ``--drain`` execute them
    from this (non-worker) process."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_output import _err
    if getattr(args, "priority_operation", None):
        refusal = _non_worker_refusal()
        if refusal:
            return _err(refusal)
    with kbc.connect_closing() as conn:
        if getattr(args, "priority_operation", None):
            from hermes_cli.kanban_priority_cli import run
            return run(conn, args)
        processed = None
        if getattr(args, "settled", False):
            req = get_request(conn, args.id) if getattr(args, "id", None) else None
            if req is None:
                return _err("--settled needs --id of an existing request")
            return 0 if req["status"] in TERMINAL else 1
        if getattr(args, "operator_log", False):
            from hermes_cli import kanban_operator_call as kox
            print(json.dumps(kox.list_calls(conn, task=args.task), indent=2))
            return 0
        if args.drain:
            try:
                processed = drain(conn, board=os.environ.get("HERMES_KANBAN_BOARD") or None, role="cli")
            except SupervisorRequestError as exc:
                return _err(str(exc))
        rows = list_requests(conn, requester=args.task, status=args.status)
        if getattr(args, "id", None):
            rows = [r for r in rows if r["id"] == args.id]
    if args.json:
        print(json.dumps({"drained": processed, "requests": rows}, indent=2))
        return 0
    if processed is not None:
        print(f"executor {processed['executor']}: processed {processed['processed'] or 'nothing'}")
    for r in rows:
        ops = ", ".join(op_label(o) for o in r["ops"] or [])
        print(f"#{r['id']} {r['status']:<8} {r['requester_task_id']} [{ops}]"
              + (f" error: {r['error']}" if r.get("error") else ""))
    return 0
