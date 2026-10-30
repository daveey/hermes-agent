"""Book a worker lost to its profile owner's DEATH like one lost to a restart. [daveey]

2026-10-30 12:26 PDT, David-MBP: gateway 43117 segfaulted. Its 4 in-flight managed runs exited 1 a few
seconds later ("[owner admission settled interrupted] Kanban owner refused: Gateway inaccessible:
service_identity_unverified"); three shared that fingerprint, so the sweep's systemic rule tripped them
straight into sticky triage on their first failure. A clean restart of the same gateway books the same runs
``owner_restart_lost`` and charges nothing; an unclean death is no more the cards' fault.

A crashed run is rebooked as ``owner_restart_lost`` (``infrastructure: true``, no failure counted, respawn
spaced by ``check_respawn_guard``) when BOTH hold:

- the worker reports its owner unavailable (``OWNER_GONE_MARKERS`` in the run error or worker output), and
- the owner named by the run's ``owner_admitted`` receipt is gone: the owner's lifecycle ledger
  (``logs/gateway-exit-diag.log``) records that pid's life as UNCLEAN, or the pid is dead/recycled.

Crash-loop guard: a card that was also in flight at the owner's PREVIOUS unclean death is charged normally
(``owner_death_repeat``), because a card whose own work kills the gateway has to stop.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

# Lower-case substrings of what a worker says when its owner went away under it.
OWNER_GONE_MARKERS = (
    "service_identity_unverified",
    "owner admission settled interrupted",
)
# A ``worker_lost`` note counts only when its reason names the owner going away: the live owner records
# ``managed_worker_failed`` & co. for a worker that died on its own, which stays the card's crash.
_OWNER_GONE_LOSS = re.compile(r"managed worker lost: [^\]]*(owner|gateway|shutdown|restart)")
_EXIT_DIAG_TAIL_BYTES = 256 * 1024
_RUN_SCAN_LIMIT = 20


def unclean_owner_deaths(owner_home: Path) -> list[int]:
    """Prior pids the owner's ledger recorded as UNCLEAN exits, oldest first. ``[]`` when unreadable."""
    path = owner_home / "logs" / "gateway-exit-diag.log"
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _EXIT_DIAG_TAIL_BYTES))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    deaths: list[int] = []
    for line in data.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("tag") != "gateway.previous_unclean_exit":
            continue
        try:
            pid = int(record["prior_pid"])
        except (KeyError, TypeError, ValueError):
            continue
        if not deaths or deaths[-1] != pid:
            deaths.append(pid)
    return deaths


def _reports_owner_gone(dead) -> bool:
    text = f"{dead.error_text}\n{dead.event_payload.get('worker_output') or ''}".lower()
    return any(marker in text for marker in OWNER_GONE_MARKERS) or bool(_OWNER_GONE_LOSS.search(text))


def _earlier_owner_death_pids(conn, task_id: str) -> list[int]:
    """``owner_death_pid`` of the card's closed runs, newest first (``None`` for a run without one)."""
    out = []
    for row in conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY id DESC LIMIT ?",
        (task_id, _RUN_SCAN_LIMIT),
    ):
        try:
            meta = json.loads(row[0]) if row[0] else {}
        except (TypeError, ValueError):
            meta = {}
        pid = meta.get("owner_death_pid") if isinstance(meta, dict) else None
        out.append(int(pid) if isinstance(pid, int) else None)
    return out


def _previous_death_seen(conn, task_id: str, owner_pid: int, deaths: list[int]) -> Optional[int]:
    """The owner's previous unclean death this card was ALSO in flight at, or None."""
    earlier = _earlier_owner_death_pids(conn, task_id)
    if owner_pid in deaths:
        idx = deaths.index(owner_pid)
        previous = deaths[idx - 1] if idx > 0 else None
    else:
        previous = deaths[-1] if deaths else None
    if previous is not None:
        return previous if previous in earlier else None
    # No ledger to order the deaths: the card's last closed run was lost to another owner death.
    if earlier and earlier[0] is not None and earlier[0] != owner_pid:
        return earlier[0]
    return None


def note(dead, conn, task_id: Optional[str], run_id) -> None:
    """Rebook ``dead`` (``_DeadWorker``) lost to its owner's death as ``owner_restart_lost``. No-op for
    any other death; a crash-loop repeat stays a crash and is tagged so the systemic rule skips it."""
    if conn is None or not task_id or dead.kind in ("clean_exit", "rate_limited", "owner_restart", "oom_killed"):
        return
    if (dead.protocol_violation or dead.rate_limited or dead.terminal_provider or dead.host_reboot
            or dead.owner_unavailable or dead.owner_restart or dead.reboot_culprit):
        return
    if not _reports_owner_gone(dead):
        return
    from hermes_cli.kanban_owner_recovery import _owner_process_gone, _owner_receipt
    receipt = _owner_receipt(conn, task_id)
    if not receipt:
        return
    try:
        owner_pid = int(receipt["pid"])
        owner_home = Path(receipt["db"]).parent
    except (KeyError, TypeError, ValueError):
        return
    deaths = unclean_owner_deaths(owner_home)
    unclean = owner_pid in deaths
    if not unclean and not _owner_process_gone(receipt):
        return  # the owner that admitted this run is still alive: not an owner death
    payload = dead.event_payload
    payload["owner_death_pid"] = owner_pid
    payload["owner_exit"] = "unclean" if unclean else "pid_changed"
    repeat = _previous_death_seen(conn, task_id, owner_pid, deaths)
    if repeat is not None:
        payload["owner_death_repeat"] = repeat
        dead.error_text = (
            f"{dead.error_text} (owner pid {owner_pid} died with this card in flight, as pid {repeat} did "
            "before it: counted as a failure)")
        return
    original = dead.error_text
    dead.kind, dead.owner_restart, dead.event_kind = "owner_restart", True, "owner_restart_lost"
    payload.update(exit_kind="owner_restart", owner_restart=True, infrastructure=True)
    how = "an unclean exit" if unclean else "its pid changed since the claim"
    dead.error_text = (
        f"pid {payload.get('pid')} lost its turn to the death of its profile owner (pid {owner_pid}, {how}) — "
        f"requeued without counting a failure; worker said: {original}")
