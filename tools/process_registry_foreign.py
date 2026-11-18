"""process_manage for a job another live Hermes process on this host supervises. [daveey]

Re-adoption after a restart is routed by the checkpoint entry's session-key shape: a bare chat id
goes to the Desktop-owned backend (``process_registry_readopt``). On a host whose gateway hosts the
Desktop chats (hermes-hub), the chat's turns run in the gateway while a separate Desktop-owned backend
re-adopts the chat's job, so the chat's ``poll``/``wait``/``kill`` looked only in the gateway's
registry and got ``not_found`` for a live job (proc_1372c59c5218, 2026-11-13). The live entry is in
the host's shared ``processes.json`` either way: this serves those actions from it, for the owning
chat only, without taking the job over. The supervising process keeps reading the job's log and
still records the exit and sends the one completion notice.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

# Process-manage actions a foreign-supervised job answers; stdin needs the supervisor's pipe.
FOREIGN_ACTIONS = frozenset({"poll", "log", "wait", "kill"})
_WAIT_STEP_SECONDS = 1.0
# How long a wait keeps reading after the PID went for the supervisor's receipt to land.
_SETTLE_GRACE_SECONDS = 10.0


def _caller_owns(entry: dict[str, Any]) -> bool:
    """The calling chat owns *entry* (same rule, compression lineage included, as retained results)."""
    from gateway.session_context import get_session_env
    from tools.process_registry_results import _owns_result
    owner = get_session_env("HERMES_SESSION_ID", "")
    try:
        return bool(owner) and _owns_result(owner, entry.get("parent_session_id"))
    except Exception:  # health: allow BLE001 -- an unreadable lineage (state.db) never grants access
        return False


def foreign_entry(registry, session_id: str) -> Optional[dict[str, Any]]:
    """The live checkpoint entry for *session_id* (exact id, or a unique ``proc_`` prefix) that
    another live Hermes process supervises and the calling chat owns; None otherwise."""
    from tools.environments.sandbox_scope import sandbox_visible
    from tools.process_registry import _checkpoint_path
    query = str(session_id or "").strip()
    if not query:
        return None
    try:
        entries = json.loads(_checkpoint_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    entries = [e for e in entries if isinstance(e, dict) and isinstance(e.get("session_id"), str)]
    exact = [e for e in entries if e["session_id"] == query]
    if not exact:
        prefix = query if query.startswith("proc_") else f"proc_{query}"
        if len(prefix) - len("proc_") < registry._MIN_PREFIX_CHARS:
            return None
        exact = [e for e in entries if e["session_id"].startswith(prefix)]
    if len(exact) != 1:
        return None
    entry = exact[0]
    pid = entry.get("pid")
    if (not isinstance(pid, int) or pid <= 0 or entry.get("pid_scope", "host") != "host"
            or not sandbox_visible(entry.get("sandbox_key", ""))):
        return None
    # Ours to answer only while another live process supervises it: an orphan is recovery's job.
    if entry.get("writer_pid") == os.getpid() or not registry._writer_alive(entry):
        return None
    if not _caller_owns(entry):
        return None
    return entry


def _log_text(entry: dict[str, Any]) -> str:
    from tools.ansi_strip import strip_ansi
    from tools.process_registry_output_log import adoptable_log
    log = adoptable_log(entry.get("output_log"))
    if not log:
        return ""
    try:
        return strip_ansi(Path(log).read_bytes().decode("utf-8", errors="replace"))
    except OSError:
        return ""


def _head(registry, entry: dict[str, Any], running: bool) -> dict[str, Any]:
    writer = entry.get("writer_pid")
    return {
        "session_id": entry["session_id"], "command": entry.get("command", ""),
        "status": "running" if running else "exited", "pid": entry.get("pid"),
        "supervised_by_pid": writer,
        "note": (f"Supervised by another Hermes process on this host (pid {writer}) since a restart; "
                 "it records the exit and sends the completion notice. poll/log/wait/kill work from here."),
    }


def _running(registry, entry: dict[str, Any]) -> bool:
    return registry._detached_host_fate(entry.get("pid"), entry.get("host_start_time")) == "running"


def _exit_fields(entry: dict[str, Any]) -> dict[str, Any]:
    from tools.process_registry_output_log import adoptable_log, recorded_exit_code, recorded_kill_source
    log = adoptable_log(entry.get("output_log"))
    if log and (killed_by := recorded_kill_source(log)):
        return {"exit_code": -15, "completion_reason": "killed", "termination_source": killed_by}
    code = recorded_exit_code(log) if log else None
    if code is None:
        return {"exit_code": None, "completion_reason": "unknown",
                "exit_note": "exited; exit status not recorded yet (the supervising process settles it)"}
    return {"exit_code": code, "completion_reason": "exited"}


def _settled(registry, session_id: str) -> Optional[dict[str, Any]]:
    """The supervisor's recorded result, read through this registry (disk receipt), if it landed."""
    from tools.process_registry_results import load_completed_results
    if session_id not in load_completed_results(session_id):
        return None
    return registry.wait(session_id, timeout=1)


def foreign_poll(registry, entry: dict[str, Any]) -> dict[str, Any]:
    running = _running(registry, entry)
    if not running and (settled := _settled(registry, entry["session_id"])) is not None:
        return settled
    result = {**_head(registry, entry, running), "output_preview": _log_text(entry)[-1000:]}
    if isinstance(entry.get("started_at"), (int, float)):
        result["uptime_seconds"] = int(time.time() - entry["started_at"])
    if not running:
        result.update(_exit_fields(entry))
    return result


def foreign_log(registry, entry: dict[str, Any], offset: Optional[int], limit: int) -> dict[str, Any]:
    lines = _log_text(entry).splitlines()
    selected = lines[-limit:] if offset is None and limit > 0 else lines[(offset or 0):(offset or 0) + limit]
    running = _running(registry, entry)
    return {**_head(registry, entry, running), **({} if running else _exit_fields(entry)),
            "output": "\n".join(selected), "total_lines": len(lines), "showing": f"{len(selected)} lines"}


def foreign_wait(registry, entry: dict[str, Any], timeout: Optional[int]) -> dict[str, Any]:
    from tools.interrupt import consume_yield, is_interrupted
    from tools.process_wait_limit import max_wait_seconds
    max_timeout = max_wait_seconds()  # same window as ProcessRegistry.wait
    if timeout is not None and timeout <= 0:
        return {"status": "error", "error": f"timeout must be positive (got {timeout})"}
    effective = min(timeout or max_timeout, max_timeout)
    deadline = time.monotonic() + effective
    gone_at = None
    pause = threading.Event()
    while True:
        if (settled := _settled(registry, entry["session_id"])) is not None:
            return settled
        if not _running(registry, entry):
            gone_at = gone_at or time.monotonic()
            if time.monotonic() - gone_at >= _SETTLE_GRACE_SECONDS or time.monotonic() >= deadline:
                return {**_head(registry, entry, False), **_exit_fields(entry),
                        "output": _log_text(entry)[-1000:]}
        elif is_interrupted() or consume_yield(threading.current_thread().ident):
            return {"status": "interrupted", "command": entry.get("command", ""), "process_running": True,
                    "output": _log_text(entry)[-1000:],
                    "note": "User sent a new message -- wait released; the process is still running."}
        elif time.monotonic() >= deadline:
            return {"status": "timeout", "command": entry.get("command", ""), "process_running": True,
                    "output": _log_text(entry)[-1000:],
                    "timeout_note": (f"Wait window of {effective}s elapsed — the process is still running. "
                                     "This is not an error. The supervising process sends the completion notice.")}
        pause.wait(min(_WAIT_STEP_SECONDS, max(0.0, deadline - time.monotonic())) or 0.05)


def foreign_kill(registry, entry: dict[str, Any]) -> dict[str, Any]:
    """Signal the job's tree (and stop its scope) without taking it over: the supervisor's reader
    sees the exit and records it. A recycled or gone PID is never signalled. A kill marker beside the
    job's log, written before the signal, tells that reader the exit was this kill (its notice says
    killed, as for a local kill, not "status unknown"); a kill that leaves the job alive withdraws it."""
    from tools.process_registry import _stop_systemd_unit
    from tools.process_registry_output_log import adoptable_log, clear_kill, record_kill
    if not _running(registry, entry):
        return {**_head(registry, entry, False), **_exit_fields(entry), "status": "already_exited"}
    log = adoptable_log(entry.get("output_log"))
    if log:
        try:
            record_kill(log, "process.kill")
        except OSError:
            log = ""  # the supervisor then reports "status unknown"; the kill itself still goes ahead
    registry._terminate_host_pid(entry["pid"], entry.get("host_start_time"))
    unit = entry.get("systemd_unit") or ""
    if unit and not _stop_systemd_unit(unit):
        alive = _running(registry, entry)
        if log and alive:
            clear_kill(log)
        return {"status": "error", "error": f"Kill incomplete: systemd scope {unit} did not stop",
                "session_id": entry["session_id"], "scope_stop_failed": unit, "process_running": alive}
    survivors = registry._post_kill_survivors(registry._session_from_entry(entry))
    if survivors:
        if log and _running(registry, entry):
            clear_kill(log)
        alive = ", ".join(map(str, survivors))
        return {"status": "error", "error": f"Kill incomplete: {len(survivors)} process(es) still alive ({alive})",
                "session_id": entry["session_id"], "survivors": survivors, "process_running": True}
    return {**_head(registry, entry, False), "status": "killed", "completion_reason": "killed",
            "termination_source": "process.kill", "output": _log_text(entry)[-1000:]}


def foreign_action(registry, action: str, session_id: str, args: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The answer for *action* on a job another live process supervises, or None (stay not_found)."""
    if action not in FOREIGN_ACTIONS:
        return None
    entry = foreign_entry(registry, session_id)
    if entry is None:
        return None
    if action == "poll":
        return foreign_poll(registry, entry)
    if action == "log":
        return foreign_log(registry, entry, args.get("offset"), int(args.get("limit", 200) or 200))
    if action == "wait":
        return foreign_wait(registry, entry, args.get("timeout"))
    return foreign_kill(registry, entry)
