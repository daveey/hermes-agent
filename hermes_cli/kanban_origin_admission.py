"""Private strict admission for parent-executed native CLI creation.

This scope is not exposed as a tool option and grants no authority itself: it
requires the current durable interactive turn, and validates it again in the
board transaction. Legacy creation intentionally keeps optional receipts.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
import json
import os

from hermes_cli import kanban_origin_native as native

@dataclass
class _Admission:
    captured: object
    gate: object = None
    admitted: set = field(default_factory=set)


_current: ContextVar = ContextVar("strict_native_creation", default=None)


class AdmissionRefused(ValueError):
    """The task cannot be committed with its promised native origin."""


@contextmanager
def strict_local_creation(*, gate=None):
    captured = native.capture(None)
    if captured is None:
        raise AdmissionRefused("strict native creation requires a live interactive turn")
    token = _current.set(_Admission(captured, gate=gate))
    try:
        yield
    finally:
        _current.reset(token)


def active():
    return _current.get() is not None


def check_tool(args, kw):
    if active() and (args.get("placement") != "local" or args.get("origin_session")
                     or os.environ.get("HERMES_ORIGIN_SESSION")
                     or args.get("requires") or args.get("runs_on") or args.get("placement_policy")
                     or args.get("self_work") is not None or kw.get("origin_intent") is not None
                     or kw.get("selfwork_intent") is not None):
        raise AdmissionRefused("strict native creation supports only local, native-origin cards")


def tool_create(fn):
    """Do not mistake overlap's successful legacy dedup for strict admission."""
    @wraps(fn)
    def wrapped(args, **kw):
        check_tool(args, kw)
        state = _current.get()
        if state is not None:
            state.admitted.clear()
        result = fn(args, **kw)
        if state is not None:
            value = json.loads(result)
            if value.get("ok") and value.get("task_id") not in state.admitted:
                raise AdmissionRefused("strict native creation did not admit the returned task")
        return result
    return wrapped


def check_entry(conn, origin_intent):
    if active() and (conn.in_transaction or origin_intent is not None):
        raise AdmissionRefused("strict native creation requires its own local transaction")


def require_receipt(conn, task_id):
    state = _current.get()
    if state is None:
        return
    context = state.captured.validated()
    scope = native._primary_scope(conn)
    binding = native._receipt(conn, task_id)
    if context is None or scope is None or binding is None:
        raise AdmissionRefused("strict native creation lost custody or its native receipt")
    gateway, profile, board = scope
    owner = {"gateway": gateway, "profile": profile, "board": board, "task_id": task_id}
    if binding["origin"] != context["origin"] or binding["owner"] != owner:
        raise AdmissionRefused("strict native creation receipt belongs to another origin or board")
    if state.gate is not None:
        state.gate.admit(task_id)
    state.admitted.add(task_id)


def existing(conn, idempotency_key, native_origin=None):
    """Recheck every replay under the insertion lock; validate strict custody too."""
    if not idempotency_key:
        return None
    from hermes_cli.kanban_db_creation import existing_creation_key
    row = existing_creation_key(conn, idempotency_key)
    if row is not None:
        require_receipt(conn, row["id"])
        native.require_created_receipt(conn, row["id"], native_origin)
        return row["id"]
    return None


def early_existing(conn, idempotency_key, created_by, native_origin=None):
    """Keep the legacy early replay, but strict replays wait for the write lock."""
    if not idempotency_key:
        return None
    from hermes_cli.kanban_db_creation import existing_creation_key, reject_reserved_key
    reject_reserved_key(idempotency_key, created_by)
    from agent.origin_turn_custody import CapturedOrigin
    strict_native = type(native_origin) is CapturedOrigin and native._primary_scope(conn) is not None
    row = None if active() or strict_native else existing_creation_key(conn, idempotency_key)
    return row["id"] if row is not None else None
