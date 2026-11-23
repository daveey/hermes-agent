"""Trusted owner-local exact-text CAS; no lifecycle or author authority. [daveey]"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time


class TextEditResult(Exception):
    """Leave the existing write transaction before returning a refusal/replay."""

    def __init__(self, disposition, *, receipt_checked=False, event_uid=None):
        super().__init__(disposition)
        self.result = {"ok": disposition == "replayed", "disposition": disposition,
                       "receipt_checked": receipt_checked, "event_uid": event_uid}


def _text(value, limit, *, nullable=False, nonblank=False):
    if nullable and value is None:
        return
    if type(value) is not str or len(value.encode("utf-8")) > limit:
        raise ValueError("invalid text type or UTF-8 byte length")
    if nonblank and not value.strip():
        raise ValueError("blank text")


def validate_request(request):
    """Exact schema; expected content is evidence, replacement is policy input."""
    if type(request) is not dict or set(request) != {"request_id", "reason", "expected", "replacement"}:
        raise ValueError("expected request_id, reason, expected, replacement")
    _text(request["request_id"], 128, nonblank=True)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request["request_id"]):
        raise ValueError("invalid request_id token")
    _text(request["reason"], 2000, nonblank=True)
    for key in ("expected", "replacement"):
        value = request[key]
        if type(value) is not dict or set(value) != {"title", "body"}:
            raise ValueError("expected exact title/body pair")
        _text(value["title"], 4000, nonblank=True)
        _text(value["body"], 100000, nullable=True)
    if request["expected"] == request["replacement"]:
        raise ValueError("no-op request refused")
    from hermes_cli.kanban_model_neutral import validate_text
    validate_text(request["replacement"]["title"], request["replacement"]["body"])


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"nonfinite JSON constant: {value}")


def read_request(path):
    with open(path, "rb") as source:
        raw = source.read(500001)
    if len(raw) > 500000:
        raise ValueError("request exceeds 500000 bytes")
    request = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
    validate_request(request)
    return request


def _inactive(conn, task):
    if task["status"] not in {"todo", "ready", "blocked", "scheduled", "triage"}:
        return False
    if any(task[key] is not None for key in ("current_run_id", "claim_lock", "claim_expires", "worker_pid")):
        return False
    if conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL LIMIT 1",
                    (task["id"],)).fetchone():
        return False
    return not (task.get("lease_holder") is not None and
                (task.get("lease_expires") is None or task["lease_expires"] > time.time()))


class TextCAS:
    """Internal hook; only edit_task's existing transaction may call check/audit."""

    def __init__(self, request):
        self.request = json.loads(json.dumps(request))
        self.event_uid = None
        self.gateway = None

    def fields(self, supplied):
        from hermes_cli.kanban_review_required import worker_identity
        if worker_identity():
            raise TextEditResult("worker_refused")
        allowed = {"title", "body", "expected_body", "home_open_only", "text_cas", "conn", "task_id"}
        for key, value in supplied.items():
            if key not in allowed and not (
                    value is None or (key == "require_lower_reason" and value is False)):
                raise ValueError("text CAS cannot mix other edit options")
        if (supplied["title"] != self.request["replacement"]["title"] or
                supplied["body"] != self.request["replacement"]["body"] or
                supplied["expected_body"] != self.request["expected"]["body"] or
                supplied["home_open_only"] is not True):
            raise ValueError("text CAS parameters disagree")
        validate_request(self.request)
        return dict(self.request["replacement"])

    def check(self, conn, task_id):
        from hermes_cli import kanban_db_sync as sync
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise TextEditResult("not_found")
        task = dict(row)
        state = sync.sync_state(conn)
        configured = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_sync_state'").fetchone()
        if configured and (not state or state["mode"] != "primary" or not state["gateway_id"] or
                           task.get("created_on") != state["gateway_id"]):
            raise TextEditResult("foreign_owner")
        self.gateway = state["gateway_id"] if state else None
        if not _inactive(conn, task):
            raise TextEditResult("active_custody")
        current = {key: task[key] for key in ("title", "body")}
        self._replay(conn, task_id, current)
        if current != self.request["expected"]:
            raise TextEditResult("stale_preimage", receipt_checked=True)

    def _replay(self, conn, task_id, current):
        from hermes_cli.kanban_completion_refs import origin_of
        columns = {row[1] for row in conn.execute("PRAGMA table_info(task_events)")}
        uid = "uid" if "uid" in columns else "NULL AS uid"
        rows = conn.execute(f"SELECT id,{uid},payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
                            (task_id, "text_cas_edited"))
        wanted = self.payload()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (ValueError, TypeError):
                raise TextEditResult("malformed_receipt", receipt_checked=True) from None
            if type(payload) is not dict:
                raise TextEditResult("malformed_receipt", receipt_checked=True)
            if payload.get("request_id") != self.request["request_id"]:
                continue
            if origin_of(conn, row["id"]) is not None or any(
                    payload.get(key) != wanted[key] for key in ("request_id", "reason", "before", "after")):
                raise TextEditResult("request_id_conflict", receipt_checked=True)
            if not row["uid"]:
                raise TextEditResult("malformed_receipt", receipt_checked=True)
            if current != self.request["replacement"]:
                raise TextEditResult("replay_state_drift", receipt_checked=True)
            raise TextEditResult("replayed", receipt_checked=True, event_uid=row["uid"])

    def payload(self):
        return {"request_id": self.request["request_id"], "reason": self.request["reason"],
                "before": self.request["expected"], "after": self.request["replacement"],
                "process_context": {"authority_model": "trusted-operator", "worker_markers_present": False,
                                    "session_hint": os.environ.get("HERMES_SESSION_ID"), "gateway": self.gateway}}

    def audit(self, conn, task_id):
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_sync import uuid7
        columns = {row[1] for row in conn.execute("PRAGMA table_info(task_events)")}
        if "uid" not in columns:
            conn.execute("ALTER TABLE task_events ADD COLUMN uid TEXT")
        kb._append_event(conn, task_id, "text_cas_edited", self.payload())
        row = conn.execute("SELECT id,uid FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                           (task_id, "text_cas_edited")).fetchone()
        self.event_uid = row["uid"] or uuid7()
        if row["uid"] is None:
            conn.execute("UPDATE task_events SET uid=? WHERE id=?", (self.event_uid, row["id"]))


def edit_text(conn, task_id, request):
    """Trusted non-worker operator API, not authentication by marker absence.

    Unknown outcomes retain the exact request identity: read target and history,
    never mint a replacement request or automatically retry. Local commit does
    not prove sync convergence. No lifecycle, leases or admission are changed.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_review_required import worker_identity
    if worker_identity():
        return {"ok": False, "disposition": "worker_refused", "receipt_checked": False}
    try:
        validate_request(request)
        _text(task_id, 64, nonblank=True)
    except (ValueError, TypeError, RecursionError) as exc:
        return {"ok": False, "disposition": "invalid_request", "receipt_checked": False, "error": str(exc)}
    hook = TextCAS(request)
    try:
        applied = kb.edit_task(conn, task_id, **request["replacement"],
                               expected_body=request["expected"]["body"], home_open_only=True, text_cas=hook)
    except TextEditResult as exc:
        return {**exc.result, "request_id": request["request_id"]}
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        return {"ok": False, "disposition": "unknown", "request_id": request["request_id"], "error": str(exc)}
    return {"ok": applied, "disposition": "applied" if applied else "not_found",
            "request_id": request["request_id"], "receipt_checked": applied, "event_uid": hook.event_uid}


def command(args):
    """CLI entry skips init/activity side effects before validation."""
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_review_required import worker_identity
    if worker_identity():
        print(json.dumps({"ok": False, "disposition": "worker_refused", "receipt_checked": False}))
        return 2
    try:
        request = read_request(args.request_file)
    except (OSError, ValueError, RecursionError) as exc:
        print(json.dumps({"ok": False, "disposition": "invalid_request", "error": str(exc)}))
        return 2
    try:
        with connect_closing() as conn:
            result = edit_text(conn, args.task_id, request)
    except (sqlite3.Error, OSError, RuntimeError, ValueError) as exc:
        result = {"ok": False, "disposition": "unknown", "request_id": request["request_id"], "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1
