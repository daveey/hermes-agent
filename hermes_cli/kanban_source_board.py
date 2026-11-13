"""workspace_source v1 (core PR-2): the synchronous Board side of a source card's run.

* ``source_protocol: 1`` rides ONE grant request: the dispatcher's own claim (:func:`protocol_call` around it), never
  the CLI ``hermes kanban claim`` or any other caller (v4.1 R3-a: per call, not process-global).
* ``/leases source_attach`` / ``publish_source`` / ``release`` (with a receipt) are synchronous ``/leases`` actions
  (v3 S2), never outbox ops. A ``400`` naming the action list is an old Board (``board_lacks_source``, v4 S2): this
  process stops sending the protocol and receipts.
* ``kanban_source_runs`` (local, never synced) records which local runs attached and from what observation, so the
  ending preflight (worker process) and the pre-spawn abort (dispatcher) agree on what this run joined.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from hermes_cli import kanban_db_sync as sync

_log = logging.getLogger(__name__)

SOURCE_PROTOCOL = 1
_PROTOCOL_CALL: contextvars.ContextVar[bool] = contextvars.ContextVar("source_protocol_call", default=False)
_BOARD_LACKS_SOURCE = {"flag": False}


class SourceCallRefused(Exception):
    """The Board rejected a source action (``reason`` is its code: ``lease_lost``, ``custody_attached``, ...)."""

    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code or "rejected", detail or ""
        super().__init__(f"{self.code}: {self.detail}" if self.detail else self.code)


class BoardLacksSource(SourceCallRefused):
    """An old Board: it does not know the source actions (``400 action must be one of ...``)."""

    def __init__(self, detail: str = ""):
        super().__init__("board_lacks_source", detail)


# --- source_protocol: per dispatcher call ----------------------------------------------------------------------------

@contextlib.contextmanager
def protocol_call() -> Iterator[None]:
    """Mark the dispatcher's own claim: only grants made inside it may carry ``source_protocol``."""
    token = _PROTOCOL_CALL.set(True)
    try:
        yield
    finally:
        _PROTOCOL_CALL.reset(token)


def grant_protocol() -> Optional[int]:
    """``source_protocol`` for this grant: only inside :func:`protocol_call`, only when enabled (which implies the
    protocol is complete), and never once this process learned the Board lacks the source actions."""
    if not _PROTOCOL_CALL.get() or _BOARD_LACKS_SOURCE["flag"]:
        return None
    from hermes_cli.kanban_workspace_source import settings
    return SOURCE_PROTOCOL if settings().enabled else None


def board_lacks_source() -> bool:
    return _BOARD_LACKS_SOURCE["flag"]


def mark_board_lacks_source(why: str) -> None:
    if not _BOARD_LACKS_SOURCE["flag"]:
        _log.warning("workspace_source: the Board lacks the source protocol (%s); this process stops sending "
                     "source_protocol and receipts (legacy behaviour)", why)
    _BOARD_LACKS_SOURCE["flag"] = True


def reset_board_lacks_source() -> None:  # tests
    _BOARD_LACKS_SOURCE["flag"] = False


# --- /leases calls ------------------------------------------------------------------------------------------------

def call(conn: sqlite3.Connection, action: str, task_id: str, run_uid: Optional[str], **body: Any) -> dict:
    """One synchronous ``/leases`` source action. Raises :class:`SourceCallRefused` (Board said no),
    :class:`BoardLacksSource` (old Board) or ``sync.LeaseUnavailable`` (no answer: the outcome is unknown)."""
    from hermes_cli.kanban_sync_client import HubError
    state = sync._state(conn)
    hub = getattr(sync._client(), "hub", None)
    if hub is None:
        raise sync.LeaseUnavailable("the lease client has no Board route for source actions")
    payload = {"action": action, "task": task_id, **({"run_uid": run_uid} if run_uid else {}), **body,
               "client_op_id": sync.uuid7()}
    try:
        res = hub.leases(state["board_id"], payload)
    except HubError as exc:
        detail = str(getattr(exc, "detail", "") or exc)
        if getattr(exc, "status", None) == 400 and "action must be one of" in detail:
            mark_board_lacks_source(f"{action}: {detail[:120]}")
            raise BoardLacksSource(detail[:300]) from exc
        raise sync.LeaseUnavailable(f"{action}: {exc}") from exc
    except OSError as exc:
        raise sync.LeaseUnavailable(f"{action}: {exc}") from exc
    if not isinstance(res, dict) or res.get("status") != "applied":
        res = res if isinstance(res, dict) else {}
        raise SourceCallRefused(str(res.get("reason") or "rejected"), str(res.get("detail") or "")[:300])
    return res


def attach(conn: sqlite3.Connection, task_id: str, run_uid: str, *, mode: str, manifest_revision: int) -> dict:
    return call(conn, "source_attach", task_id, run_uid, mode=mode, manifest_revision=int(manifest_revision))


def publish(conn: sqlite3.Connection, task_id: str, run_uid: str, *, expected_revision: int, manifest: dict) -> dict:
    return call(conn, "publish_source", task_id, run_uid, expected_revision=int(expected_revision), manifest=manifest)


def release(conn: sqlite3.Connection, task_id: str, run_uid: str, **fields: Any) -> dict:
    """The sync ``/leases release`` with the outbox release's own fields (policy + ``source_receipt``)."""
    return call(conn, "release", task_id, run_uid, **fields)


def renew_remaining(conn: sqlite3.Connection, task_id: str, run_uid: str, ttl_s: int) -> float:
    """The publish lease fence (v2 §4 step 5): renew synchronously and return the seconds left on the lease.
    Raises :class:`SourceCallRefused` ``lease_lost`` when the Board did not renew it."""
    state = sync._state(conn)
    try:
        lease = sync._client().renew(board_id=state["board_id"], task_uid=task_id, run_uid=run_uid,
                                     gateway_id=state["gateway_id"], ttl_s=max(int(ttl_s), 1))
    except sync._CLIENT_ERRORS as exc:
        raise SourceCallRefused("lease_lost", f"no Board: {exc}") from exc
    if not lease.granted or not lease.expires_ms:
        raise SourceCallRefused("lease_lost", f"renewal refused ({lease.reason or 'not granted'})")
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        conn.execute("UPDATE tasks SET lease_expires = ?, lease_version = COALESCE(?, lease_version) WHERE id = ?",
                     (lease.expires_ms, lease.version, task_id))
    return lease.expires_ms / 1000 - time.time()


# --- attached runs (local, never synced) --------------------------------------------------------------------------

_RUNS_DDL = ("CREATE TABLE IF NOT EXISTS kanban_source_runs (run_id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, "
             "run_uid TEXT NOT NULL, mode TEXT NOT NULL, manifest_revision INTEGER NOT NULL, branch TEXT, "
             "path TEXT, pre_head TEXT, manifest TEXT, state TEXT NOT NULL, at INTEGER NOT NULL)")


@dataclass(frozen=True)
class AttachedRun:
    run_id: int
    task_id: str
    run_uid: str
    mode: str
    manifest_revision: int
    branch: Optional[str]
    path: Optional[str]
    pre_head: Optional[str]
    manifest: Optional[dict]
    state: str  # pending (asked, answer unknown) | attached


def record_run(conn: sqlite3.Connection, run: AttachedRun) -> None:
    from hermes_cli.kanban_db_connect import write_txn
    with write_txn(conn):
        conn.execute(_RUNS_DDL)
        conn.execute("INSERT OR REPLACE INTO kanban_source_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (run.run_id, run.task_id, run.run_uid, run.mode, int(run.manifest_revision), run.branch,
                      run.path, run.pre_head, json.dumps(run.manifest) if run.manifest is not None else None,
                      run.state, int(time.time())))


def attached_run(conn: sqlite3.Connection, run_id: Optional[int]) -> Optional[AttachedRun]:
    if run_id is None:
        return None
    try:
        row = conn.execute("SELECT run_id, task_id, run_uid, mode, manifest_revision, branch, path, pre_head, "
                           "manifest, state FROM kanban_source_runs WHERE run_id = ?", (int(run_id),)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return AttachedRun(int(row[0]), row[1], row[2], row[3], int(row[4]), row[5], row[6], row[7],
                       json.loads(row[8]) if row[8] else None, row[9])


def update_run(conn: sqlite3.Connection, run_id: int, **changes: Any) -> None:
    from hermes_cli.kanban_db_connect import write_txn
    sets = ", ".join(f"{k} = ?" for k in changes)
    values = [json.dumps(v) if k == "manifest" and v is not None else v for k, v in changes.items()]
    with write_txn(conn):
        conn.execute(_RUNS_DDL)
        conn.execute(f"UPDATE kanban_source_runs SET {sets} WHERE run_id = ?", (*values, int(run_id)))
