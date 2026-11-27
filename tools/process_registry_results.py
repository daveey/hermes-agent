"""Bounded, profile-local receipts for completed terminal processes.

Receipts are read through process_manage, never replayed as notifications or
adopted as live PIDs. Each producer writes its own file so independent one-shot
parents cannot overwrite each other's results in the running-PID checkpoint.
"""

import contextlib
import contextvars
import json
import logging
import re
import sqlite3
import time
from hermes_dir_acl import make_user_dir

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger("tools.process_registry")

RESULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_RETAINED_RESULTS = 64
_RESULT_FIELDS = (
    "id", "command", "cwd", "task_id", "owner_task_id", "session_key",
    "parent_session_id", "started_at", "exit_code", "completion_reason",
    "termination_source", "notify_on_complete",
)


def _result_paths(*, strict: bool = False):
    """Prune by completion time, not start time (jobs can take days)."""
    directory = get_hermes_home() / "logs" / "process-results"
    cutoff = time.time() - RESULT_RETENTION_SECONDS
    retained = []
    if not strict:
        for ack in directory.glob("proc_*.ack"):
            try:
                if ack.stat().st_mtime < cutoff and not ack.with_suffix(".json").exists():
                    ack.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
    if strict:
        # Path.glob may suppress enumeration errors. An absent results directory
        # is normal before the first completion; other errors are not absence.
        try:
            paths = [p for p in directory.iterdir() if p.name.startswith("proc_") and p.suffix == ".json"]
        except FileNotFoundError:
            paths = []
    else:
        paths = directory.glob("proc_*.json")
    for path in paths:
        try:
            modified = path.stat().st_mtime
            if modified < cutoff:
                if not strict:
                    path.unlink(missing_ok=True)
                    path.with_suffix(".ack").unlink(missing_ok=True)
            else:
                retained.append((modified, path))
        except FileNotFoundError:
            continue  # Another producer pruned it: expiry, in strict reads too.
    retained.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    visible = [path for _, path in retained[:MAX_RETAINED_RESULTS]]
    for _, path in retained[MAX_RETAINED_RESULTS:]:
        if _owed(path, strict=strict):  # retained custody must also remain retrievable [daveey]
            visible.append(path)
        elif not strict:
            path.unlink(missing_ok=True)
            path.with_suffix(".ack").unlink(missing_ok=True)
    return visible


def _owed(path, *, strict: bool = False) -> bool:
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return False  # pruned since it was listed
    except (OSError, ValueError):
        if strict:
            raise
        return False
    return isinstance(record, dict) and isinstance(record.get("delivery_obligation"), dict)


def save_completed_result(session, obligation: dict | None = None) -> None:
    """``obligation``: the completion is still owed to a gateway watcher (``process_registry_obligations``);
    it rides this same atomic write, so it is durable before the checkpoint entry goes. [daveey]"""
    if session.sandbox_key:
        return  # Hosted command/output belong to the tenant, never this host's logs.
    from agent.redact import redact_sensitive_text, redact_terminal_output
    from tools.process_registry import MAX_OUTPUT_CHARS

    with session._lock:
        record = {key: getattr(session, key) for key in _RESULT_FIELDS}
        record["output"] = session.output_buffer[-MAX_OUTPUT_CHARS:]
    if obligation:
        record["delivery_obligation"] = obligation
    # Live-output opt-out must not persist raw credentials in durable receipts.
    record["output"] = redact_terminal_output(record["output"], record["command"], force=True)
    record["command"] = redact_sensitive_text(record["command"], code_file=True, force=True)
    directory = get_hermes_home() / "logs" / "process-results"
    try:
        from hermes_constants import assert_named_profile_home_live
        assert_named_profile_home_live(directory)
        make_user_dir(directory, mode=0o700, parents=True, exist_ok=True)
        atomic_json_write(directory / f"{session.id}.json", record, mode=0o600)
        _result_paths()
    except OSError:
        # Preserve live delivery on disk failure, but never silently claim durability.
        logger.warning("Could not retain completed process result %s", session.id, exc_info=True)


def record_consumed(session) -> None:
    """Persist an output acknowledgement separately: a late result rewrite cannot erase it."""
    if session.sandbox_key:
        return
    directory = get_hermes_home() / "logs" / "process-results"
    try:
        make_user_dir(directory, mode=0o700, parents=True, exist_ok=True)
        atomic_json_write(directory / f"{session.id}.ack", {
            "id": session.id, "started_at": session.started_at,
            "parent_session_id": session.parent_session_id,
        }, mode=0o600)
    except OSError:
        logger.warning("Could not retain process acknowledgement %s", session.id, exc_info=True)


def completion_consumed(session) -> bool:
    """Only an exact process incarnation/owner acknowledgement clears a status failure."""
    path = get_hermes_home() / "logs" / "process-results" / f"{session.id}.ack"
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return False
    return record == {"id": session.id, "started_at": session.started_at,
                      "parent_session_id": session.parent_session_id}


def status_exit_fields(session, *, strict: bool) -> dict:
    fields = {"exit_code": session.exit_code, "exited_at": session.exited_at,
              "completion_reason": session.completion_reason}
    if strict:
        fields["acknowledged"] = completion_consumed(session)
    return fields


_lineage_pass: contextvars.ContextVar = contextvars.ContextVar("process_result_lineage_pass", default=None)


@contextlib.contextmanager
def _owner_pass():
    """Share one read-only lineage handle and per-parent tips across one inventory pass.

    Every outcome refresh walks up to MAX_RETAINED_RESULTS receipts twice (load, then the
    strict owner filter); opening a SessionDB per receipt cost ~128 opens per refresh, each
    a GIL release in the serve. Reentrant: a nested pass reuses the outer one. The memo only
    lives for the pass, so a later rotation is seen by the next pass.
    """
    if _lineage_pass.get() is not None:
        yield
        return
    state = {"db": None, "tips": {}}
    token = _lineage_pass.set(state)
    try:
        yield
    finally:
        _lineage_pass.reset(token)
        if state["db"] is not None:
            state["db"].close()


def _owns_result(owner: str, parent: str | None) -> bool:
    if not parent:
        return False
    if owner == parent:
        return True
    from hermes_state import SessionDB

    # Pure lineage read on the hot path of every retained-result load; a writable open here
    # was one more writer handle per call inside the gateway (#100896).
    state = _lineage_pass.get()
    if state is None:
        db = SessionDB(read_only=True)
        try:
            return db.get_compression_tip(parent) == owner
        finally:
            db.close()
    tips = state["tips"]
    if parent not in tips:
        if state["db"] is None:
            state["db"] = SessionDB(read_only=True)
        tips[parent] = state["db"].get_compression_tip(parent)
    return tips[parent] == owner


def registry_sessions(registry, include_retained, strict, owner_session_id, loader) -> dict:
    """Opt-in qualified status view; preserve the default tool/display inventory."""
    with _owner_pass():
        return _registry_sessions(registry, include_retained, strict, owner_session_id, loader)


def _registry_sessions(registry, include_retained, strict, owner_session_id, loader) -> dict:
    from tools.environments.sandbox_scope import sandbox_visible

    if strict and (not include_retained or not owner_session_id):
        raise ValueError("strict process inventory requires retained results and explicit owner")
    sessions = (loader(strict=True, owner_session_id=owner_session_id) if strict
                else loader()) if include_retained else {}
    with registry._lock:
        sessions.update(registry._finished)
        sessions.update(registry._running)
    if not strict:
        return sessions
    selected = {}
    for key, session in sessions.items():
        if not session.parent_session_id:
            raise ValueError("process owner scope unavailable")
        if not _owns_result(owner_session_id, session.parent_session_id):
            continue
        if not sandbox_visible(session.sandbox_key):
            raise ValueError("process owner scope unavailable")
        refreshed = registry._refresh_detached_session(session)
        if refreshed is None or registry._uncollected_gone(refreshed):
            raise ValueError("current process missing exit evidence")
        if refreshed.exited and type(refreshed.exit_code) is not int:
            raise ValueError("current process missing exit evidence")
        selected[key] = refreshed
    return selected


def load_completed_results(prefix: str = "", *, strict: bool = False, owner_session_id: str | None = None) -> dict:
    """Restore read-only snapshots; no process handles, watchers, or queue events."""
    with _owner_pass():
        return _load_completed_results(prefix, strict=strict, owner_session_id=owner_session_id)


def _load_completed_results(prefix: str, *, strict: bool, owner_session_id: str | None) -> dict:
    from tools.process_registry import ProcessSession

    from gateway.session_context import get_session_env
    from tools.environments.sandbox_scope import hosted_scope

    owner = owner_session_id if owner_session_id is not None else get_session_env("HERMES_SESSION_ID", "")
    if strict and (not owner_session_id or hosted_scope() is not None):
        raise ValueError("process owner scope unavailable")
    # A hosted agent's processes leave no receipt here and it sees no local one: never read or prune them.
    if not owner or hosted_scope() is not None:
        return {}
    results = {}
    try:
        paths = _result_paths(strict=True) if strict else _result_paths()
    except OSError:
        if strict:
            raise
        logger.warning("Could not read retained process results", exc_info=True)
        return results
    for path in paths:
        if not path.stem.startswith(prefix):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
            if record["id"] != path.stem or not re.fullmatch(r"proc_[\w]+", record["id"]):
                if strict:
                    raise ValueError("malformed process identity")
                continue
            if strict and not isinstance(record.get("parent_session_id"), str):
                raise ValueError("process owner scope unavailable")
            if not _owns_result(owner, record.get("parent_session_id")):
                continue
            if strict and type(record.get("exit_code")) is not int:
                raise ValueError("current process missing exit evidence")
            session = ProcessSession(
                **{key: record[key] for key in _RESULT_FIELDS},
                # Receipts written before processes carried a sandbox are a local activity's.
                sandbox_key=record.get("sandbox_key", ""), exited=True, output_buffer=record["output"],
            )
            if strict:  # receipts carry no exit time; their mtime is the completion time pruning already uses
                session.exited_at = path.stat().st_mtime
            session._completion_event.set()
            results[session.id] = session
        except FileNotFoundError:
            continue  # pruned by another producer after it was listed: expiry, not unreadable evidence
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            if strict:
                raise
            logger.debug("Skipping unreadable process result %s", path.name, exc_info=True)
    return results
