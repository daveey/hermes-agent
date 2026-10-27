"""Read-only ownership snapshot for the abandonment guard.

``ownership_snapshot`` answers one question without side effects: does the exact
owner task still have a live continuation AND a result consumer able to return to
the exact origin request? ``live`` needs every positive proof; anything absent,
stale, ambiguous or changed between two reads is ``unknown``. Never ``abandoned``:
absence of evidence is not evidence of abandonment.

Every store is opened ``query_only``. Nothing here refreshes observations, adopts,
claims, migrates, sends a courier request or starts a poller. Remote owners are
read through the existing hub courier: the owner runs ``owner_evidence`` itself
(``hermes kanban ownership-snapshot``) and the origin validates the reply with
``ownership_snapshot(owner_evidence=...)``; a model- or courier-supplied field is
never trusted without the scope, binding and expiry checks below.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

SCHEMA = 1
OWNER_EVIDENCE_TTL = 30.0  # seconds an owner reading may be used by the origin
_OWNER_KEYS = ("gateway", "profile", "board", "task_id")


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _open(path: Path) -> sqlite3.Connection:
    """Open an existing store strictly ``mode=ro``; never create, migrate, or fall back to rw.

    The shared ``_ro`` fallback reopens ``mode=rw``, and closing the last rw connection on
    a WAL store can checkpoint and delete the WAL: a write. An unreadable store is
    ``unknown`` here (the sqlite error is caught by the caller), not a reason to write.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    conn.row_factory = sqlite3.Row
    return conn


def _unknown(reason: str, **extra) -> dict:
    return {"schema": SCHEMA, "state": "unknown", "reasons": [reason], **extra}


def _fresh_number(value, now: float, ttl: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and 0 <= now - value <= ttl


def _running(conn, task, profile: str, now: float) -> dict:
    from hermes_cli.kanban_db_dispatch import _worker_alive
    from hermes_cli.kanban_host_identity import is_local_claim

    run = conn.execute("SELECT * FROM task_runs WHERE id=? AND task_id=?",
                       (task["current_run_id"], task["id"])).fetchone()
    started = run["worker_started_at"] if run else None
    if (not run or run["profile"] != profile or run["status"] != "running"
            or run["ended_at"] is not None or run["outcome"] is not None
            or not run["claim_expires"] or run["claim_expires"] <= now
            or not is_local_claim(run["claim_lock"]) or not run["worker_pid"] or started is None
            or not _worker_alive(run["worker_pid"], started)):
        raise ValueError("owner_live_continuation_unverified")
    return {"kind": "running", "run_id": run["id"], "claim_expires": run["claim_expires"],
            "pid": run["worker_pid"], "worker_started_at": str(started)}


def _scheduled(conn, task, board_path: Path, profile: str, now: float) -> dict:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_continuation import pending_wake

    wake = pending_wake(conn, task["id"]) or {}
    when = wake.get("wake_at")
    if (type(when) not in (int, float) or not math.isfinite(when) or when <= now  # type: ignore[arg-type]
            or wake.get("resume_status") == "review"):
        raise ValueError("owner_scheduled_wake_unverified")
    from gateway.kanban_dispatcher_health import available
    if not available(kb.kanban_home() / "kanban", board_path, profile, "ready"):
        raise ValueError("owner_dispatcher_unverified")
    if conn.execute("SELECT 1 FROM task_links WHERE child_id=? LIMIT 1", (task["id"],)).fetchone():
        # A wake does not own a blocked DAG; dependency proof is not part of this contract.
        raise ValueError("owner_dependency_unverified")
    return {"kind": "scheduled", "wake_at": when, "dispatcher": True}


def owner_evidence(owner: dict, binding_id: str, *, now: float | None = None) -> dict:
    """Owner-native reading, produced ON the owner host. Never raises; never writes."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import get_active_profile_name
    from hermes_constants import get_hermes_home

    now = time.time() if now is None else now
    try:
        profile, board = owner["profile"], owner["board"]
        home = get_hermes_home().resolve()
        path = home / "kanban.db" if board == "default" else home / "kanban/boards" / board / "kanban.db"
        if (profile != get_active_profile_name() or kb.kanban_home().resolve() != home
                or kb.kanban_db_path(board).resolve() != path or not path.is_file()):
            return _unknown("owner_scope_unavailable")
        with closing(_open(path)) as conn:
            conn.execute("BEGIN")
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (owner["task_id"],)).fetchone()
            if task is None or task["assignee"] != profile or task["completed_at"] is not None:
                return _unknown("owner_task_scope_or_state_mismatch")
            markers = [json.loads(r[0]) for r in conn.execute(
                "SELECT binding_json FROM origin_creation_intents WHERE task_id=?", (task["id"],))]
            marker = next((m for m in markers if m.get("binding_id") == binding_id
                           and m.get("owner") == {**owner}), None)
            if marker is None:
                return _unknown("owner_binding_missing")
            try:
                if task["status"] == "running":
                    continuation = _running(conn, task, profile, now)
                elif task["status"] == "scheduled":
                    continuation = _scheduled(conn, task, path, profile, now)
                else:
                    raise ValueError("owner_wait_state_unverified")
            except (ValueError, ImportError) as exc:
                return _unknown(str(exc) if isinstance(exc, ValueError) else "owner_dispatcher_unverified",
                                owner=owner, observed_at=now)
            return {"schema": SCHEMA, "state": "live", "reasons": [], "owner": owner,
                    "binding": {"binding_id": binding_id, "version": marker["version"],
                                "hash": digest(_json(marker))},
                    "body_digest": digest(task["body"] or ""), "status": task["status"],
                    "continuation": continuation, "observed_at": now}
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return _unknown("owner_readback_unavailable")


def _validate_owner(evidence, owner: dict, binding_id: str, now: float) -> str | None:
    """Reason string when a (possibly remote) owner reading is unusable, else None."""
    if not isinstance(evidence, dict) or evidence.get("schema") != SCHEMA:
        return "owner_evidence_invalid"
    if evidence.get("state") != "live":
        reason = (evidence.get("reasons") or [None])[0]
        return reason if isinstance(reason, str) and reason.startswith(("origin_", "owner_")) and len(reason) < 80 \
            else "owner_evidence_unknown"
    binding, cont = evidence.get("binding"), evidence.get("continuation")
    if (evidence.get("owner") != owner or not isinstance(binding, dict)
            or binding.get("binding_id") != binding_id or not isinstance(cont, dict)):
        return "owner_evidence_scope_mismatch"
    if not _fresh_number(evidence.get("observed_at"), now, OWNER_EVIDENCE_TTL):
        return "owner_evidence_stale"
    if cont.get("kind") == "running":
        if not (type(cont.get("claim_expires")) in (int, float) and cont["claim_expires"] > now
                and type(cont.get("pid")) is int and cont.get("worker_started_at")):
            return "owner_live_continuation_unverified"
    elif cont.get("kind") == "scheduled":
        if not (type(cont.get("wake_at")) in (int, float) and cont["wake_at"] > now
                and cont.get("dispatcher") is True):
            return "owner_scheduled_wake_unverified"
    else:
        return "owner_live_continuation_unverified"
    return None


def _binding_generation(ledger: Path, owner: dict, binding_id: str) -> dict:
    from hermes_cli.kanban_origin_ledger import _binding_key
    with closing(_open(ledger)) as conn:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT binding_hash,binding_json,resolved_session,observation_version,released "
            "FROM origin_obligations WHERE binding_id=?",
            (_binding_key({"owner": owner, "binding_id": binding_id}),)).fetchone()
    if row is None:
        raise ValueError("origin_obligation_missing")
    binding = json.loads(row["binding_json"])
    return {"binding_id": binding_id, "version": binding["version"], "hash": row["binding_hash"],
            "publication_version": binding["publication_version"],
            "observation_version": row["observation_version"], "released": bool(row["released"]),
            "origin": binding["origin"], "resolved_session": row["resolved_session"]}


def _newest_request(state_db: Path, session: str) -> dict:
    with closing(_open(state_db)) as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT id,content FROM messages WHERE session_id=? AND role='user' "
                           "ORDER BY id DESC LIMIT 1", (session,)).fetchone()
    if row is None or not isinstance(row["content"], str):
        raise ValueError("origin_request_unavailable")
    return {"uid": str(row["id"]), "digest": digest(row["content"])}


def _consumer(home: Path, session: str, board: Path | None) -> dict:
    if board is None:
        return {"available": False, "epoch": None, "incarnation": None, "age": None}
    from tui_gateway.kanban_consumer_health import snapshot
    return snapshot(home, session, board)


def _compose(*, ledger, state_db, owner, binding_id, request, home, board, evidence, now) -> dict:
    gen = _binding_generation(ledger, owner, binding_id)
    session = gen["resolved_session"]
    reasons = []
    if gen["released"]:
        reasons.append("origin_obligation_released")
    if not isinstance(evidence, dict) or evidence.get("binding", {}).get("hash") is None:
        reasons.append("owner_evidence_invalid")
    else:
        bound = evidence["binding"]
        # The owner's marker has no publication_version; compare generation, not the ledger hash.
        if bound.get("version") != gen["version"]:
            reasons.append("binding_generation_mismatch")
        if evidence.get("body_digest") != request.get("digest"):
            reasons.append("owner_request_digest_mismatch")
    current = _newest_request(state_db, session)
    if current != {"uid": request.get("uid"), "digest": request.get("digest")}:
        reasons.append("origin_request_superseded")
    consumer = _consumer(home, session, board)
    if not consumer["available"]:
        reasons.append("result_consumer_unverified")
    return {"gen": gen, "request": current, "consumer": consumer, "reasons": reasons}


def ownership_snapshot(*, ledger: Path, state_db: Path, gateway: str, owner: dict, binding_id: str,
                       request: dict, home: Path, consumer_board: Path | None = None,
                       owner_evidence_in: dict | None = None, expected_consumer_epoch: int | None = None,
                       now: float | None = None) -> dict:
    """Compose binding generation, owner continuation and return-consumer health.

    ``owner`` is the qualified gateway/profile/board/task_id; ``request`` is the origin
    request ``{uid, digest}`` the guard believes is current (``digest`` is the sha256 of
    the exact request text, which must equal the owner task body). ``gateway`` is the
    trusted local gateway: only an owner on it is read locally; any other owner needs
    ``owner_evidence_in`` produced by that owner and carried by the hub courier.
    ``expected_consumer_epoch`` lets a caller detect invalidation between its own reads.
    """
    now = time.time() if now is None else now
    try:
        if not isinstance(owner, dict) or set(owner) != set(_OWNER_KEYS) or not isinstance(request, dict):
            return _unknown("snapshot_request_invalid")
        # A local owner is always read locally: supplied evidence for it would be forgeable.
        evidence = None if owner["gateway"] == gateway else owner_evidence_in
        if owner["gateway"] == gateway:
            evidence = owner_evidence(owner, binding_id, now=now)
        if evidence is None:
            return _unknown("owner_evidence_absent", owner=owner)
        reason = _validate_owner(evidence, owner, binding_id, now)
        if reason:
            return _unknown(reason, owner=owner)
        first = _compose(ledger=ledger, state_db=state_db, owner=owner, binding_id=binding_id,
                         request=request, home=home, board=consumer_board, evidence=evidence, now=now)
        # Recheck after composition: a Stop, replacement, refusal or new request in
        # between must not be reported as the earlier healthy state.
        second_now = time.time() if now is None else now
        if owner["gateway"] == gateway:  # re-read the local owner: claim/PID/body may have changed
            evidence = owner_evidence(owner, binding_id, now=second_now)
            reason = _validate_owner(evidence, owner, binding_id, second_now)
            if reason:
                return _unknown("changed_during_read", owner=owner)
        second = _compose(ledger=ledger, state_db=state_db, owner=owner, binding_id=binding_id,
                          request=request, home=home, board=consumer_board, evidence=evidence, now=second_now)
        reasons = list(first["reasons"])
        if (first["gen"] != second["gen"] or first["request"] != second["request"]
                or first["consumer"]["epoch"] != second["consumer"]["epoch"]
                or first["consumer"]["incarnation"] != second["consumer"]["incarnation"]):
            reasons.append("changed_during_read")
        if (expected_consumer_epoch is not None
                and second["consumer"]["epoch"] != expected_consumer_epoch):
            reasons.append("consumer_epoch_changed")
        return {"schema": SCHEMA, "state": "unknown" if reasons else "live", "reasons": reasons,
                "owner": owner, "binding": second["gen"], "request": second["request"],
                "continuation": evidence["continuation"], "owner_observed_at": evidence["observed_at"],
                "consumer": second["consumer"], "observed_at": now}
    except (OSError, sqlite3.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        code = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(("origin_", "owner_")) \
            else "snapshot_unavailable"
        return _unknown(code)
