"""Whether this host's kanban sync loop is running, where the dispatcher, ``sync status`` and create can read it. [daveey]

Incident (2026-11-02, MacBook): the gateway logged ``kanban sync: not starting: board default: ...`` once and its
sync watcher never respun. With sync enabled every claim goes through the Board lease, so the board stalled (52
outbox ops unpushed, 15 ready, 0 running) while the "dispatcher stuck" warning named only ``claim_deferred`` counts,
``hermes kanban sync status`` printed ``last_error: None``, and ``kanban_create(placement=<machine>)`` kept returning
local ``runs_on`` rows that could only reach the Board through the stopped loop.

``run_forever`` records ``{running: false, since, reason}`` here when it refuses to start or exits; its first
completed round clears the file. Readers only trust it while ``kanban.sync.enabled`` is on.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "SyncNotRunning", "clear", "describe", "down", "explicit_runs_on", "path", "placement_runs_on", "record_down",
    "stranded_create",
]

_FILE = ".sync-state.json"


class SyncNotRunning(ValueError):
    """A create refused because its ``runs_on`` row would strand behind the stopped sync (``rejection:
    sync_not_running``, the same as the placement path)."""


def path() -> Path:
    """Beside the host's ``.sync.lock`` (one sync loop per host)."""
    from hermes_cli import kanban_db as kb
    return kb.kanban_home() / "kanban" / _FILE


def record_down(reason: str) -> None:
    """The loop is not running. Keeps the first ``since`` while the reason is unchanged (a respun refusal)."""
    target = path()
    previous = _read(target)
    since = time.time()
    if previous and previous.get("running") is False and previous.get("reason") == reason:
        since = float(previous.get("since") or since)
    record = {"running": False, "since": since, "reason": str(reason), "pid": os.getpid()}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{_FILE}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(record), encoding="utf-8")
        temp.replace(target)
    except OSError:
        from hermes_cli.kanban_sync_client import _log
        _log.warning("kanban sync: cannot record the sync loop state at %s", target, exc_info=True)


def clear() -> None:
    """A round completed: the loop runs. One stat per round when nothing was recorded."""
    target = path()
    try:
        if target.exists():
            target.unlink()
    except OSError:
        pass


def _read(target: Path) -> Optional[dict[str, Any]]:
    try:
        record = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def down() -> Optional[dict[str, Any]]:
    """The recorded stop while sync is enabled, else None (no record, or sync is off so nothing waits on it)."""
    try:
        from hermes_cli import kanban_db_sync as sync
        if not sync.sync_enabled():
            return None
    except Exception:  # health: allow BLE001 -- a diagnostic read must never break the dispatcher or create
        return None
    record = _read(path())
    if not record or record.get("running") is not False:
        return None
    return record


def _when(since: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(float(since)))
    except (TypeError, ValueError, OverflowError):
        return "an unknown time"


def describe(record: Optional[dict[str, Any]] = None) -> str:
    """``kanban sync not running since <time>: <reason>``, or ``""`` when sync runs (or is off)."""
    record = down() if record is None else record
    if not record:
        return ""
    return f"kanban sync not running since {_when(record.get('since'))}: {record.get('reason') or 'unknown reason'}"


def placement_runs_on(conn: Any, placement: Any) -> Optional[list[str]]:
    """``kanban_worker_node.placement_runs_on``, except None while sync is down: a machine pin then takes the
    gateway route (the placement relay) instead of a local ``runs_on`` row nothing can push. [daveey]

    Raises ``MachinePinRefused`` (``placement_unavailable``) when the machine is on Board ``hold``: it imports
    nothing while held (11-11: a ``placement="worker-1"`` create returned ok during a migration hold and the card
    sat unclaimed 48 min). Not checked while sync is down: the gateway route is unchanged there."""
    if down():
        return None
    from hermes_cli import kanban_worker_node as wn
    labels = wn.placement_runs_on(conn, placement)
    if labels is not None:
        wn.check_machine_pins(conn, labels)
    return labels


def explicit_runs_on(conn: Any, value: Any, *, courier: bool = False) -> Optional[list[str]]:
    """``kanban_worker_node.explicit_runs_on`` (machine pins need pickup), and ValueError for a row the stopped
    sync would strand."""
    from hermes_cli import kanban_worker_node as wn
    labels = wn.explicit_runs_on(conn, value)
    stranded = stranded_create(labels, courier=courier)
    if stranded:
        raise SyncNotRunning(stranded)
    return labels


def stranded_create(runs_on: Optional[list[str]], *, courier: bool = False) -> str:
    """Why a create must not make this ``runs_on`` row (it reaches its machine only through this host's stopped
    sync), else ``""``. A courier create keeps its own retry. 2026-11-02: placement="hub" cards stranded. [daveey]"""
    stopped = describe() if runs_on and not courier else ""
    if not stopped:
        return ""
    return (f"Not created: this card needs runs_on={runs_on}, which reaches its machine only through this host's "
            f"Board sync, and {stopped}. Use placement='<gateway>' to relay it to a gateway, or retry once "
            "`hermes kanban sync status` shows running: yes. Nothing changed.")
