"""Durable create-and-first-turn identity, owned by the session store.

No network callbacks, agent imports or ambient profile selection live here. Runtime
adapters must claim before scheduling and never replay a claimed turn on recovery.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import time
import uuid
import threading
from typing import Callable, Protocol


class StartRuntime(Protocol):
    """The owning runtime admits an already-persisted turn; it never creates one.

    schedule must adopt user_row_id/first_turn_id and call finish only after a
    terminal receipt. Raising after claim is ambiguous, not permission to retry.
    The service and store must outlive all scheduled terminal callbacks.
    """

    def check_supported(self, owner: StartOwner) -> None: ...

    def schedule(self, row: dict, intent: StartIntent, finish: Callable[[str], bool]) -> None: ...


class GatewayStartRuntime:
    """Adapter to the existing in-process Desktop/TUI build and turn lifecycle."""

    def __init__(self, server, profile_home):
        self.server, self.profile_home = server, profile_home

    def check_supported(self, owner: StartOwner) -> None:
        import os
        server = self.server
        with server._profile_build_scope(self.profile_home):
            if server._turn_isolation_enabled() or os.environ.get("HERMES_COMPUTE_HOST_CHILD") == "1":
                raise NotImplementedError("durable starts require the owning in-process runtime")
        expected = server._response_profile_name(owner.profile)
        if expected != owner.profile or server._profile_home(owner.profile) != self.profile_home:
            raise PermissionError("start owner profile does not match runtime")

    def schedule(self, row: dict, intent: StartIntent, finish: Callable[[str], bool]) -> None:
        from agent.context_compressor import _DB_PERSISTED_MARKER
        server = self.server
        sid, key = uuid.uuid4().hex, row["stored_session_id"]
        with server._profile_build_scope(self.profile_home):
            record = server._deferred_session_record(
                key, cols=80, cwd=server._default_session_cwd(), history=[], lease=None,
                source=intent.source, profile_home=self.profile_home)
        record["auth_user_id"] = row["principal"]
        record["_submit_user_row"] = {
            "role": "user", "content": intent.text, "message_uid": row["first_turn_id"],
            "_row_id": row["user_row_id"], _DB_PERSISTED_MARKER: True, "_submit_row_session_id": key}
        # A concurrent human resume may already have mounted this row. Never
        # replace its history or inject into that runtime. Leave this start
        # explicitly failed rather than dispatching a second competing turn.
        # Reserve before publication: prompt.submit sees running=True from the
        # first instant this record can be resumed, so it queues rather than
        # replacing our staged first row or scheduling a competing initial turn.
        error, _ = server._lock_in_submit_turn(None, sid, record, intent.text, {}, False, None, None, None)
        if error:
            finish("failed")
            return
        if server._claim_or_reuse_live(sid, key, record, None) is not None:
            finish("failed")
            return
        server._schedule_agent_build(sid)

        def terminal(receipt):
            status = receipt.get("status")
            state = {"settled": "completed", "failed": "failed", "cancelled": "cancelled"}.get(status)
            if state is None:
                raise ValueError("unrecognized terminal start receipt")
            if not finish(state):
                raise RuntimeError("terminal start receipt was not committed")

        def run():
            error = server._wait_agent_for_prompt(record, None, sid)
            if error:
                with record["history_lock"]:
                    record["running"] = False
                    server._clear_inflight_turn(record)
                finish("failed")
                return
            with record["history_lock"]:
                cancelled = record.get("_turn_cancel_requested") or not record.get("running")
            if cancelled:
                finish("cancelled")
                return
            if not server._run_prompt_submit(None, sid, record, intent.text, terminal_callback=terminal):
                finish("failed")

        if server._start_session_work(run, name=f"start-{sid}", session=record) is None:
            with record["history_lock"]:
                record["running"] = False
                server._clear_inflight_turn(record)
            finish("failed")


def get_service(profile: str) -> StartService:
    """Plugin import path; import lazily to use server's bound runtime namespace."""
    from tui_gateway import server
    return server.get_session_start_service(profile)


class StartService:
    """In-process plugin contract; no HTTP callbacks or client-supplied prompts.

    Register per authoritative owner (including authenticated principal). A
    resolver must authorize each call, including retries/status, and return the
    immutable trusted intent. Unloading a plugin revokes new access. Python
    plugins are trusted code, not a sandbox; never expose register over RPC.
    """

    def __init__(self, store: StartStore, runtime: StartRuntime):
        self.store, self.runtime = store, runtime
        self._resolvers = {}
        self._lock = threading.RLock()

    def register(self, owner: StartOwner, namespace: str,
                 resolver: Callable[[StartOwner, str], StartIntent]) -> Callable[[], None]:
        _key(owner, namespace, "registration")
        if not callable(resolver):
            raise TypeError("start resolver must be callable")
        key = (owner, namespace)
        registration = (object(), resolver)
        with self._lock:
            if key in self._resolvers:
                raise ValueError("start namespace already registered for owner")
            self._resolvers[key] = registration

        def unregister():
            with self._lock:
                if self._resolvers.get(key) is registration:
                    del self._resolvers[key]
        return unregister

    def _resolve(self, owner: StartOwner, namespace: str, request_id: str) -> StartIntent:
        _key(owner, namespace, request_id)
        with self._lock:
            registration = self._resolvers.get((owner, namespace))
            if registration is None:
                raise PermissionError("start request service not registered for owner")
            intent = registration[1](owner, request_id)
        if not isinstance(intent, StartIntent):
            raise TypeError("start resolver must return StartIntent")
        return intent

    def start(self, owner: StartOwner, namespace: str, request_id: str) -> dict:
        intent = self._resolve(owner, namespace, request_id)
        existing = self.store.lookup(owner, namespace, request_id)
        if existing is None or existing["state"] == "queued":
            self.runtime.check_supported(owner)
        # accept also checks immutable intent on retries; completed/ambiguous
        # starts remain readable when this process can no longer run new turns.
        row = self.store.accept(owner=owner, namespace=namespace, request_id=request_id, intent=intent)
        token = uuid.uuid4().hex
        if self.store.claim(row["first_turn_id"], token):
            self.runtime.schedule(row, intent, lambda state: self.store.finish(row["first_turn_id"], token, state))
        status = self.store.status(owner, namespace, request_id)
        if status is None:
            raise RuntimeError("accepted start mapping disappeared")
        return status

    def status(self, owner: StartOwner, namespace: str, request_id: str) -> dict | None:
        self._resolve(owner, namespace, request_id)
        return self.store.status(owner, namespace, request_id)


@dataclass(frozen=True)
class StartOwner:
    gateway_id: str
    profile: str
    principal: str


@dataclass(frozen=True)
class StartIntent:
    title: str
    text: str
    source: str = "desktop"

    def __post_init__(self):
        if not isinstance(self.title, str) or not 1 <= len(self.title) <= 500:
            raise ValueError("start title must contain 1–500 characters")
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 100_000:
            raise ValueError("start text must contain 1–100000 characters")
        if self.source not in ("desktop", "tui"):
            raise ValueError("unsupported start source")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_start_requests (
    gateway_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    principal TEXT NOT NULL,
    namespace TEXT NOT NULL,
    request_id TEXT NOT NULL,
    intent_digest TEXT NOT NULL,
    stored_session_id TEXT NOT NULL UNIQUE,
    first_turn_id TEXT NOT NULL UNIQUE,
    user_row_id INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','completed','failed','cancelled')),
    runtime_token TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(gateway_id, profile, principal, namespace, request_id)
)
"""


def _key(owner: StartOwner, namespace: str, request_id: str) -> tuple[str, ...]:
    values = (owner.gateway_id, owner.profile, owner.principal, namespace, request_id)
    if any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in values):
        raise ValueError("start identity fields must contain 1–256 characters")
    return values


_WHERE = "gateway_id=? AND profile=? AND principal=? AND namespace=? AND request_id=?"


class StartStore:
    """An additive ledger in state.db; all writes use its existing writer/lock policy.

    Ledger rows intentionally survive session deletion. Deleting/archiving a chat
    is never permission to recreate or re-send a previously accepted request.
    """
    def __init__(self, db):
        self.db = db
        db._execute_write(lambda conn: conn.execute(_SCHEMA))

    def lookup(self, owner: StartOwner, namespace: str, request_id: str) -> dict | None:
        row = self.db._read_one("SELECT * FROM session_start_requests WHERE " + _WHERE,
                                _key(owner, namespace, request_id))
        return dict(row) if row is not None else None

    def accept(self, *, owner: StartOwner, namespace: str, request_id: str, intent: StartIntent) -> dict:
        key = _key(owner, namespace, request_id)
        digest = hashlib.sha256(json.dumps(asdict(intent), sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        session_id, turn_id = uuid.uuid4().hex, uuid.uuid4().hex
        now = time.time()
        # Use the canonical transcript serializer and counters, in the SAME
        # writer transaction as the visible session and request mapping.
        from hermes_state_messages import _INSERT_MESSAGE_SQL
        msg = {"content": intent.text, "message_uid": turn_id}
        params = self.db._message_row_params(session_id, "user", msg, None, now, keep_reasoning=True)

        def write(conn):
            existing = conn.execute("SELECT * FROM session_start_requests WHERE " + _WHERE, key).fetchone()
            if existing is not None:
                if existing["intent_digest"] != digest:
                    raise ValueError("start request idempotency conflict")
                return dict(existing)
            from hermes_state_titles import next_title_in_lineage
            title = next_title_in_lineage(conn, self.db.sanitize_title(intent.title) or "Session")
            conn.execute(
                "INSERT INTO sessions (id, source, user_id, profile_name, title, started_at) VALUES (?,?,?,?,?,?)",
                (session_id, intent.source, owner.principal, owner.profile, title, now))
            row_id = conn.execute(_INSERT_MESSAGE_SQL, params).lastrowid
            self.db._bump_session_counters(conn, session_id, 1, 0, unit=True)
            conn.execute(
                "INSERT INTO session_start_requests VALUES (?,?,?,?,?,?,?,?,?,'queued',NULL,?,?)",
                (*key, digest, session_id, turn_id, row_id, now, now))
            return dict(conn.execute("SELECT * FROM session_start_requests WHERE " + _WHERE, key).fetchone())
        return self.db._execute_write(write, patience_s=self.db._TRANSCRIPT_WRITE_PATIENCE_S)

    def status(self, owner: StartOwner, namespace: str, request_id: str) -> dict | None:
        """Owner-local reconciliation; missing runtime/expired lease never means finished.

        Active-turn is a durable lease observation, not a promise of process health.
        A claimed turn without a terminal receipt remains ambiguous and cannot replay.
        """
        start = self.lookup(owner, namespace, request_id)
        if start is None:
            return None
        sid = start["stored_session_id"]
        tip = self.db.get_compression_tip(sid) or sid
        row = self.db.get_session(tip)
        root = self.db._session_turn_lease_key(tip)
        lease = self.db._read_one(
            "SELECT expires_at FROM session_turn_leases WHERE conversation_id=?", (root,))
        active = lease is not None and lease["expires_at"] > time.time()
        state = start["state"]
        return {
            "gateway_id": owner.gateway_id, "profile": owner.profile,
            "stored_session_id": sid, "resolved_session_id": tip,
            "first_turn_id": start["first_turn_id"], "first_turn_state": state,
            "exists": row is not None, "archived": bool(row and row.get("archived")),
            "active_turn": active,
            "reconciliation": "unknown_outcome" if state == "running" and not active else state,
        }

    def claim(self, first_turn_id: str, runtime_token: str) -> bool:
        """Only queued -> running may schedule. No time-based takeover or replay."""
        if not runtime_token:
            raise ValueError("runtime token required")
        def write(conn):
            row = conn.execute("SELECT * FROM session_start_requests WHERE first_turn_id=? AND state='queued'",
                               (first_turn_id,)).fetchone()
            if row is None:
                return False
            sid = row["stored_session_id"]
            session = conn.execute("SELECT archived, ended_at FROM sessions WHERE id=?", (sid,)).fetchone()
            messages = conn.execute("SELECT id, message_uid FROM messages WHERE session_id=? LIMIT 2", (sid,)).fetchall()
            pristine = (session is not None and not session["archived"] and session["ended_at"] is None
                        and len(messages) == 1 and messages[0]["id"] == row["user_row_id"]
                        and messages[0]["message_uid"] == first_turn_id)
            if not pristine:
                conn.execute("UPDATE session_start_requests SET state='cancelled', updated_at=? WHERE first_turn_id=?",
                             (time.time(), first_turn_id))
                return False
            conn.execute("UPDATE session_start_requests SET state='running', runtime_token=?, updated_at=? "
                         "WHERE first_turn_id=?", (runtime_token, time.time(), first_turn_id))
            return True
        return bool(self.db._execute_write(write))

    def finish(self, first_turn_id: str, runtime_token: str, state: str) -> bool:
        if state not in ("completed", "failed", "cancelled"):
            raise ValueError("invalid terminal start state")
        return bool(self.db._execute_write(lambda conn: conn.execute(
            "UPDATE session_start_requests SET state=?, updated_at=? "
            "WHERE first_turn_id=? AND runtime_token=? AND state='running'",
            (state, time.time(), first_turn_id, runtime_token)).rowcount))
